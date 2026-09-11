import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
HELPER = REPOSITORY / "helper" / "discover.py"
sys.path.insert(0, str(REPOSITORY / "helper"))

from omatree_core import discovery, protocol, scanner
from omatree_core.resources import (
    NdjsonBudget,
    ResourceLimitExceeded,
    ResourceLimits,
    check_path,
)
from omatree_core.snapshot import SnapshotBuilder, snapshot_from_events

import importlib.util

SPEC = importlib.util.spec_from_file_location("security_discover", HELPER)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def limits(**overrides):
    values = {
        "max_directories": 10,
        "max_entries": 20,
        "max_hardlink_identities": 10,
        "max_path_bytes": 4096,
        "max_event_bytes": 1024,
        "max_total_ndjson_bytes": 8192,
        "max_discovered_filesystems": 10,
    }
    values.update(overrides)
    return ResourceLimits(**values)


def scan(path, resource_limits):
    reporter = scanner.ScanReporter("r", lambda _event: None, limits=resource_limits)
    records = []
    result = scanner.scan_tree(
        str(path), set(), reporter, scanner.Cancellation(), records.append,
        resource_limits,
    )
    return result, records


class ScannerResourceTests(unittest.TestCase):
    def test_directory_limit_accepts_exact_and_rejects_one_over(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "child").mkdir()
            self.assertEqual(scan(root, limits(max_directories=2))[0]["directoryCount"], 2)
            with self.assertRaisesRegex(ResourceLimitExceeded, "directories"):
                scan(root, limits(max_directories=1))

    def test_entry_limit_accepts_exact_and_rejects_one_over(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "file").write_text("data", encoding="utf-8")
            scan(root, limits(max_entries=2))
            with self.assertRaisesRegex(ResourceLimitExceeded, "scanned entries"):
                scan(root, limits(max_entries=1))

    def test_hardlink_limit_accepts_exact_and_never_disables_deduplication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first"
            second = root / "second"
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            os.link(first, root / "first-link")
            os.link(second, root / "second-link")
            with self.assertRaisesRegex(ResourceLimitExceeded, "hardlink identities"):
                scan(root, limits(max_hardlink_identities=1))

            one = Path(temp) / "only"
            one.mkdir()
            original = one / "data"
            original.write_text("data", encoding="utf-8")
            os.link(original, one / "link")
            result, _records = scan(one, limits(max_hardlink_identities=1))
            expected = os.stat(one).st_blocks * 512 + os.stat(original).st_blocks * 512
            self.assertEqual(result["bytes"], expected)

    def test_path_limit_is_checked_before_a_child_is_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child = root / "child"
            child.mkdir()
            exact = len(os.fsencode(str(child)))
            scan(root, limits(max_path_bytes=exact))
            with self.assertRaisesRegex(ResourceLimitExceeded, "path bytes"):
                scan(root, limits(max_path_bytes=exact - 1))


class SnapshotAndStreamResourceTests(unittest.TestCase):
    @staticmethod
    def record(path, parent=None):
        return {
            "path": path, "parentPath": parent, "name": Path(path).name,
            "bytes": 0, "directFilesBytes": 0,
            "childDirectoryCount": 0, "warningCount": 0,
        }

    def test_snapshot_rejects_offending_directory_before_retaining_it(self):
        builder = SnapshotBuilder(limits(max_directories=1))
        builder.add_directory(self.record("/root"))
        with self.assertRaisesRegex(ResourceLimitExceeded, "directories"):
            builder.add_directory(self.record("/root/child", "/root"))
        self.assertEqual(list(builder._directories), ["/root"])

    def test_snapshot_path_limit_is_exact(self):
        path = "/root"
        check_path(path, limits(max_path_bytes=len(path.encode())))
        builder = SnapshotBuilder(limits(max_path_bytes=len(path.encode()) - 1))
        with self.assertRaisesRegex(ResourceLimitExceeded, "path bytes"):
            builder.add_directory(self.record(path))
        self.assertFalse(builder._directories)

    def test_ndjson_event_and_total_limits_are_exact(self):
        encoded = protocol.encode_ndjson(protocol.progress_event("r", 1, 0))
        size = len(encoded.encode())
        budget = NdjsonBudget(limits(max_event_bytes=size, max_total_ndjson_bytes=size * 2))
        budget.account(encoded)
        budget.account(encoded, terminal=True)
        with self.assertRaisesRegex(ResourceLimitExceeded, "total NDJSON bytes"):
            budget.account(encoded, terminal=True)
        with self.assertRaisesRegex(ResourceLimitExceeded, "encoded event bytes"):
            NdjsonBudget(limits(max_event_bytes=size - 1)).account(encoded)

    def test_resource_limited_cli_never_emits_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "child").mkdir()
            args = argparse.Namespace(
                mountpoint=str(root), path=str(root), request_id="limited"
            )
            events = []
            findmnt = {"filesystems": [{"target": str(root)}]}
            with mock.patch.object(helper, "run_json", return_value=findmnt), \
                    mock.patch.object(helper, "emit_ndjson", side_effect=events.append):
                code = helper.scan_command(args, limits(max_directories=1))
            self.assertEqual(code, 3)
            self.assertEqual(events[-1]["type"], "error")
            self.assertIn("Resource limit exceeded", events[-1]["error"])
            self.assertNotIn("complete", [event["type"] for event in events])
            with self.assertRaisesRegex(Exception, "did not complete successfully"):
                snapshot_from_events(events)

    def test_discovery_filesystem_limit_accepts_exact_and_rejects_over(self):
        mounts = {
            "filesystems": [
                {"target": "/one", "source": "one", "fstype": "ext4"},
                {"target": "/two", "source": "two", "fstype": "ext4"},
            ]
        }
        capacity = lambda _path: {
            "totalBytes": 1, "usedBytes": 0, "availableBytes": 1,
            "usagePercent": 0.0,
        }
        result = discovery.build_discovery(
            {}, mounts, capacity, limits(max_discovered_filesystems=2)
        )
        self.assertEqual(len(result["filesystems"]), 2)
        with self.assertRaisesRegex(ResourceLimitExceeded, "discovered filesystems"):
            discovery.build_discovery(
                {}, mounts, capacity, limits(max_discovered_filesystems=1)
            )


class BoundedProcessTests(unittest.TestCase):
    def run_python(self, source, **kwargs):
        return discovery.run_bounded(["/usr/bin/python3", "-c", source], **kwargs)

    def test_normal_bounded_output(self):
        stdout, stderr = self.run_python(
            "import sys; print('ok'); print('note', file=sys.stderr)"
        )
        self.assertEqual(stdout, b"ok\n")
        self.assertEqual(stderr, b"note\n")

    def test_nonzero_exit_preserves_bounded_output(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_python("import sys; print('bad'); sys.exit(7)")
        self.assertEqual(caught.exception.returncode, 7)
        self.assertEqual(caught.exception.output, b"bad\n")

    def test_stdout_and_stderr_overflow_are_controlled(self):
        with self.assertRaisesRegex(discovery.BoundedProcessError, "stdout"):
            self.run_python("print('x' * 20)", stdout_limit=10)
        with self.assertRaisesRegex(discovery.BoundedProcessError, "stderr"):
            self.run_python(
                "import sys; print('x' * 20, file=sys.stderr)", stderr_limit=10
            )

    def test_timeout_terminates_and_reaps_child(self):
        seen = []
        real = discovery._terminate_and_reap

        def record(process, grace):
            real(process, grace)
            seen.append(process.poll())

        with mock.patch.object(discovery, "_terminate_and_reap", side_effect=record):
            with self.assertRaisesRegex(discovery.BoundedProcessError, "timed out"):
                self.run_python(
                    "import time; time.sleep(10)", timeout_seconds=0.05,
                    terminate_grace_seconds=0.05,
                )
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])

    def test_terminate_escalates_to_kill(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired(["cmd"], 0.01), -9]
        discovery._terminate_and_reap(process, 0.01)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_relative_executable_is_rejected(self):
        with self.assertRaisesRegex(discovery.BoundedProcessError, "absolute path"):
            discovery.run_bounded(["python3", "-c", "print('unsafe')"])

    def test_invalid_json_is_controlled(self):
        with mock.patch.object(discovery, "run_bounded", return_value=(b"not-json", b"")):
            with self.assertRaises(json.JSONDecodeError):
                discovery.run_json(["/usr/bin/findmnt"])

    def test_discovery_uses_absolute_executable_arrays(self):
        responses = [
            {"blockdevices": []},
            {"filesystems": []},
        ]
        commands = []

        def provide(command):
            commands.append(command)
            return responses.pop(0)

        with mock.patch.object(discovery, "run_json", side_effect=provide):
            discovery.discover()
        self.assertEqual(commands[0][0], "/usr/bin/lsblk")
        self.assertEqual(commands[1][0], "/usr/bin/findmnt")
        self.assertTrue(all(os.path.isabs(command[0]) for command in commands))


class ExecutableIdentityTests(unittest.TestCase):
    def test_launchers_use_package_managed_python(self):
        self.assertEqual(HELPER.read_text(encoding="utf-8").splitlines()[0], "#!/usr/bin/python3")
        launcher = (REPOSITORY / "bin" / "omatree").read_text(encoding="utf-8")
        self.assertEqual(launcher.splitlines()[0], "#!/usr/bin/python3")

    def test_tui_mount_discovery_uses_absolute_findmnt(self):
        controller = (REPOSITORY / "tui" / "controller.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"/usr/bin/findmnt"', controller)

    def test_poisoned_path_cannot_select_fake_discovery_executables(self):
        with tempfile.TemporaryDirectory() as temp:
            poison = Path(temp)
            marker = poison / "selected"
            for name in ("python3", "findmnt", "lsblk"):
                executable = poison / name
                executable.write_text(
                    "#!/bin/sh\necho selected >> '" + str(marker) + "'\nexit 99\n",
                    encoding="utf-8",
                )
                executable.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = str(poison)
            process = subprocess.run(
                ["/usr/bin/python3", str(HELPER), "discover"],
                env=environment, capture_output=True, text=True, timeout=15,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertFalse(marker.exists())
            self.assertIn("filesystems", json.loads(process.stdout))

    def test_discovery_cli_rejects_oversized_serialized_output(self):
        oversized = {"schemaVersion": 1, "filesystems": ["x" * 128]}
        stderr = mock.Mock()
        with mock.patch.object(helper, "discover", return_value=oversized), \
                mock.patch.object(helper, "DISCOVERY_STDOUT_LIMIT", 64), \
                mock.patch.object(helper.sys, "stderr", stderr), \
                mock.patch.object(helper.sys, "stdout", mock.Mock()), \
                mock.patch.object(
                    helper.argparse.ArgumentParser, "parse_args",
                    return_value=argparse.Namespace(command="discover"),
                ):
            self.assertEqual(helper.main(), 1)


if __name__ == "__main__":
    unittest.main()
