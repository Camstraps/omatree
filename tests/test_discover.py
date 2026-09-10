import importlib.util
import os
from pathlib import Path
import argparse
import tempfile
import unittest
from unittest import mock


HELPER_PATH = Path(__file__).parents[1] / "helper" / "discover.py"
SPEC = importlib.util.spec_from_file_location("omatree_discover", HELPER_PATH)
assert SPEC and SPEC.loader
discover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discover)


def fake_capacity(path):
    sizes = {
        "/": 500 * 1024**3,
        "/home": 500 * 1024**3,
        "/home/alex/ExternalSSD": 900 * 1024**3,
        "/mnt/share": 2 * 1024**4,
    }
    total = sizes[path]
    return {
        "totalBytes": total,
        "usedBytes": total // 2,
        "availableBytes": total // 2,
        "usagePercent": 50.0,
    }


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.lsblk = {
            "blockdevices": [
                {
                    "name": "nvme0n1",
                    "path": "/dev/nvme0n1",
                    "type": "disk",
                    "model": "System SSD",
                    "rm": False,
                    "children": [
                        {
                            "name": "nvme0n1p2",
                            "path": "/dev/nvme0n1p2",
                            "type": "part",
                            "fstype": "btrfs",
                        }
                    ],
                },
                {
                    "name": "nvme1n1",
                    "path": "/dev/nvme1n1",
                    "type": "disk",
                    "model": "WDC Data SSD",
                    "tran": "nvme",
                    "rm": False,
                    "children": [
                        {
                            "name": "nvme1n1p1",
                            "path": "/dev/nvme1n1p1",
                            "type": "part",
                            "fstype": "ext4",
                            "label": "External SSD",
                            "uuid": "ssd-uuid",
                        }
                    ],
                },
            ]
        }
        self.findmnt = {
            "filesystems": [
                {
                    "source": "/dev/nvme0n1p2[/@]",
                    "target": "/",
                    "fstype": "btrfs",
                    "children": [
                        {
                            "source": "proc",
                            "target": "/proc",
                            "fstype": "proc",
                        },
                        {
                            "source": "tmpfs",
                            "target": "/run",
                            "fstype": "tmpfs",
                        },
                        {
                            "source": "portal",
                            "target": "/run/user/1000/doc",
                            "fstype": "fuse.portal",
                        },
                        {
                            "source": "/dev/nvme0n1p2[/@home]",
                            "target": "/home",
                            "fstype": "btrfs",
                            "children": [
                                {
                                    "source": "/dev/nvme1n1p1",
                                    "target": "/home/alex/ExternalSSD",
                                    "fstype": "ext4",
                                }
                            ],
                        },
                        {
                            "source": "server:/data",
                            "target": "/mnt/share",
                            "fstype": "nfs4",
                        },
                    ],
                }
            ]
        }

    def test_nested_mount_is_separate_and_grouped_by_physical_disk(self):
        result = discover.build_discovery(self.lsblk, self.findmnt, fake_capacity)
        by_mount = {item["mountpoint"]: item for item in result["filesystems"]}

        self.assertIn("/home", by_mount)
        self.assertIn("/home/alex/ExternalSSD", by_mount)
        ssd = by_mount["/home/alex/ExternalSSD"]
        self.assertEqual(ssd["devicePath"], "/dev/nvme1n1p1")
        self.assertEqual(ssd["physicalDevice"], "/dev/nvme1n1")
        self.assertEqual(ssd["physicalDiskLabel"], "WDC Data SSD")
        self.assertEqual(ssd["displayName"], "External SSD")

    def test_pseudo_filesystems_are_hidden(self):
        result = discover.build_discovery(self.lsblk, self.findmnt, fake_capacity)
        mountpoints = {item["mountpoint"] for item in result["filesystems"]}
        self.assertNotIn("/proc", mountpoints)
        self.assertNotIn("/run", mountpoints)
        self.assertNotIn("/run/user/1000/doc", mountpoints)

    def test_network_filesystem_is_supported(self):
        result = discover.build_discovery(self.lsblk, self.findmnt, fake_capacity)
        share = next(item for item in result["filesystems"] if item["mountpoint"] == "/mnt/share")
        self.assertTrue(share["network"])
        self.assertEqual(share["groupId"], "network")


class ScanTests(unittest.TestCase):
    def reporter(self):
        messages = []
        return discover.ScanReporter("test", messages.append), messages

    def scan(self, path, excluded=None, cancellation=None):
        reporter, messages = self.reporter()
        result = discover.scan_directory(
            str(path),
            {str(item) for item in (excluded or set())},
            reporter,
            cancellation or discover.Cancellation(),
        )
        return result, reporter, messages

    def allocated(self, path):
        return os.stat(path, follow_symlinks=False).st_blocks * 512

    def test_normal_nested_directory_sizes_and_descending_sort(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            small = root / "small"
            large = root / "large"
            nested = large / "nested"
            small.mkdir()
            nested.mkdir(parents=True)
            (small / "one").write_bytes(b"a" * 4096)
            (nested / "two").write_bytes(b"b" * 16384)

            result, _, _ = self.scan(root)

            self.assertEqual([child["name"] for child in result["children"]], ["large", "small"])
            expected_large = (
                self.allocated(large)
                + self.allocated(nested)
                + self.allocated(nested / "two")
            )
            self.assertEqual(result["children"][0]["bytes"], expected_large)

            records = {record["path"]: record for record in result["directories"]}
            self.assertEqual(records[str(nested)]["parentPath"], str(large))
            self.assertEqual(records[str(large)]["bytes"], expected_large)
            self.assertEqual(records[str(large)]["childDirectoryCount"], 1)
            self.assertEqual(records[str(nested)]["directFilesBytes"], self.allocated(nested / "two"))
            self.assertEqual(result["directoryCount"], 4)

    def test_every_directory_is_emitted_once_in_postorder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            grandchild = root / "parent" / "child"
            grandchild.mkdir(parents=True)
            reporter, _ = self.reporter()
            records = []

            result = discover.scan_tree(
                str(root), set(), reporter, discover.Cancellation(), records.append
            )

            self.assertEqual(len(records), 3)
            self.assertEqual(len({record["path"] for record in records}), 3)
            self.assertLess(
                next(i for i, record in enumerate(records) if record["path"] == str(grandchild)),
                next(i for i, record in enumerate(records) if record["path"] == str(root)),
            )
            self.assertEqual(result["directoryCount"], 3)

    def test_symlinks_are_counted_but_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "root"
            external = base / "external"
            child = root / "child"
            child.mkdir(parents=True)
            external.mkdir()
            (external / "large").write_bytes(b"x" * 65536)
            link = child / "link"
            link.symlink_to(external, target_is_directory=True)

            result, _, _ = self.scan(root)
            expected = self.allocated(child) + self.allocated(link)
            self.assertEqual(result["children"][0]["bytes"], expected)

    def test_hard_linked_file_is_counted_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            original = first / "data"
            original.write_bytes(b"x" * 8192)
            os.link(original, second / "same-data")

            result, _, _ = self.scan(root)
            expected = (
                self.allocated(root)
                + self.allocated(first)
                + self.allocated(second)
                + self.allocated(original)
            )
            self.assertEqual(result["bytes"], expected)

    def test_descendant_mountpoint_is_pruned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ordinary = root / "ordinary"
            mounted = root / "mounted"
            ordinary.mkdir()
            mounted.mkdir()
            (ordinary / "included").write_bytes(b"i" * 4096)
            (mounted / "excluded").write_bytes(b"e" * 65536)

            result, _, _ = self.scan(root, {mounted})

            self.assertEqual([child["name"] for child in result["children"]], ["ordinary"])
            self.assertNotIn(str(mounted), {record["path"] for record in result["directories"]})
            self.assertEqual(
                result["bytes"],
                self.allocated(root) + self.allocated(ordinary) + self.allocated(ordinary / "included"),
            )

    def test_disappearing_directory_emits_warning_and_scan_continues(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gone = root / "gone"
            okay = root / "okay"
            gone.mkdir()
            okay.mkdir()
            real_scandir = os.scandir

            def flaky_scandir(path):
                if Path(path) == gone:
                    raise FileNotFoundError("disappeared during scan")
                return real_scandir(path)

            reporter, messages = self.reporter()
            with mock.patch.object(discover.os, "scandir", side_effect=flaky_scandir):
                result = discover.scan_directory(str(root), set(), reporter, discover.Cancellation())

            self.assertEqual({child["name"] for child in result["children"]}, {"gone", "okay"})
            self.assertTrue(any(message["type"] == "warning" for message in messages))
            records = {record["path"]: record for record in result["directories"]}
            self.assertGreater(records[str(gone)]["warningCount"], 0)
            self.assertGreater(records[str(root)]["warningCount"], 0)

    def test_cancellation_never_returns_a_partial_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(10):
                (root / str(index)).write_bytes(b"x" * 4096)

            class CancelAfterFirstCheck(discover.Cancellation):
                def __init__(self):
                    super().__init__()
                    self.checks = 0

                def check(self):
                    self.checks += 1
                    if self.checks > 1:
                        raise discover.ScanCancelled

            with self.assertRaises(discover.ScanCancelled):
                self.scan(root, cancellation=CancelAfterFirstCheck())

    def test_scan_command_streams_directories_before_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "child").mkdir()
            (root / "child" / "data").write_bytes(b"x" * 4096)
            messages = []
            args = argparse.Namespace(
                mountpoint=str(root), path=str(root), request_id="full-tree"
            )
            findmnt = {"filesystems": [{"target": str(root)}]}

            with mock.patch.object(discover, "run_json", return_value=findmnt), mock.patch.object(
                discover, "emit_ndjson", side_effect=messages.append
            ):
                exit_code = discover.scan_command(args)

            self.assertEqual(exit_code, 0)
            types = [message["type"] for message in messages]
            self.assertEqual(types[0], "start")
            self.assertEqual(types[-1], "complete")
            self.assertEqual(types.count("directory"), 2)
            self.assertNotIn("child", types)
            self.assertEqual(messages[-1]["directoryCount"], 2)

    def test_cancelled_scan_command_never_emits_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "child").mkdir()
            (root / "child" / "data").write_bytes(b"x" * 4096)
            messages = []
            args = argparse.Namespace(
                mountpoint=str(root), path=str(root), request_id="cancel-tree"
            )
            findmnt = {"filesystems": [{"target": str(root)}]}

            class CancelDuringScan(discover.Cancellation):
                def __init__(self):
                    super().__init__()
                    self.checks = 0

                def check(self):
                    self.checks += 1
                    if self.checks > 2:
                        raise discover.ScanCancelled

            with mock.patch.object(discover, "run_json", return_value=findmnt), mock.patch.object(
                discover, "emit_ndjson", side_effect=messages.append
            ), mock.patch.object(discover, "Cancellation", CancelDuringScan), mock.patch.object(
                discover.signal, "signal"
            ):
                exit_code = discover.scan_command(args)

            self.assertEqual(exit_code, 130)
            self.assertIn("cancelled", [message["type"] for message in messages])
            self.assertNotIn("complete", [message["type"] for message in messages])

    def test_allocated_size_is_used_for_sparse_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sparse = root / "sparse"
            with sparse.open("wb") as handle:
                handle.truncate(8 * 1024 * 1024)

            result, _, _ = self.scan(root)

            self.assertEqual(result["directFilesBytes"], self.allocated(sparse))
            self.assertLess(result["directFilesBytes"], sparse.stat().st_size)


if __name__ == "__main__":
    unittest.main()
