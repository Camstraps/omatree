from pathlib import Path
import curses
import os
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from helper.omatree_core import scanner
from helper.omatree_core.snapshot import SnapshotBuilder
from tui.app import CursesApp
from tui.controller import (
    BrowserController,
    ScanController,
    ScanPlan,
    containing_filesystem,
    make_scan_plan,
    requested_path,
)


def record(path, parent, name, bytes_, child_count):
    return {
        "path": path,
        "parentPath": parent,
        "name": name,
        "bytes": bytes_,
        "directFilesBytes": bytes_,
        "childDirectoryCount": child_count,
        "warningCount": 0,
    }


def browser_snapshot():
    builder = SnapshotBuilder()
    for item in [
        record("/root/b/leaf", "/root/b", "leaf", 10, 0),
        record("/root/b", "/root", "beta", 20, 1),
        record("/root/z", "/root", "zulu", 20, 0),
        record("/root/a", "/root", "Alpha", 20, 0),
        record("/root", None, "root", 60, 3),
    ]:
        builder.add_directory(item)
    return builder.finalize("/root", 5)


class TargetResolutionTests(unittest.TestCase):
    def test_no_argument_defaults_to_home(self):
        self.assertEqual(
            requested_path(None, {"HOME": "/home/example"}),
            "/home/example",
        )

    def test_explicit_target_is_canonicalized(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "child"
            target.mkdir()
            self.assertEqual(
                requested_path(str(target / ".." / "child"), {}),
                str(target),
            )

    def test_deepest_containing_mount_is_selected_without_prefix_collision(self):
        filesystems = [
            {"mountpoint": "/"},
            {"mountpoint": "/home"},
            {"mountpoint": "/home/user/data"},
            {"mountpoint": "/home/user/database"},
        ]
        selected = containing_filesystem("/home/user/data/project", filesystems)
        self.assertEqual(selected["mountpoint"], "/home/user/data")

    def test_scan_plan_uses_shared_nested_mount_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            nested = root / "nested"
            target.mkdir()
            nested.mkdir()

            plan = make_scan_plan(
                str(target),
                discover_provider=lambda: {
                    "schemaVersion": 1,
                    "filesystems": [{"mountpoint": str(root)}],
                },
                findmnt_provider=lambda _command: {
                    "filesystems": [{
                        "target": str(root),
                        "children": [{"target": str(nested)}],
                    }],
                },
            )

            self.assertEqual(plan.path, str(target))
            self.assertEqual(plan.mountpoint, str(root))
            self.assertEqual(plan.excluded_mountpoints, frozenset({str(nested)}))


class BrowserControllerTests(unittest.TestCase):
    def setUp(self):
        self.browser = BrowserController(browser_snapshot())

    def test_visible_tree_flattening_and_canonical_order(self):
        self.assertEqual(
            self.browser.visible_paths,
            ["/root", "/root/a", "/root/b", "/root/z"],
        )
        self.browser.move(2)
        self.browser.expand_selected()
        self.assertEqual(
            self.browser.visible_paths,
            ["/root", "/root/a", "/root/b", "/root/b/leaf", "/root/z"],
        )

    def test_expand_collapse_and_parent_navigation(self):
        self.browser.move(2)
        self.browser.expand_selected()
        self.browser.move(1)
        self.assertEqual(self.browser.selected_path, "/root/b/leaf")
        self.browser.select_parent()
        self.assertEqual(self.browser.selected_path, "/root/b")
        self.browser.collapse_selected()
        self.assertNotIn("/root/b/leaf", self.browser.visible_paths)
        self.browser.collapse_selected()
        self.assertEqual(self.browser.selected_path, "/root/b")
        self.browser.select_parent()
        self.assertEqual(self.browser.selected_path, "/root")

    def test_enter_toggle_semantics_expand_then_collapse(self):
        self.browser.move(2)
        self.browser.toggle_selected()
        self.assertIn("/root/b/leaf", self.browser.visible_paths)
        self.browser.toggle_selected()
        self.assertNotIn("/root/b/leaf", self.browser.visible_paths)

    def test_right_expand_is_idempotent_and_left_collapse_is_idempotent(self):
        self.browser.move(2)
        self.browser.expand_selected()
        self.browser.expand_selected()
        self.assertIn("/root/b/leaf", self.browser.visible_paths)
        self.browser.collapse_selected()
        self.browser.collapse_selected()
        self.assertNotIn("/root/b/leaf", self.browser.visible_paths)
        self.assertEqual(self.browser.selected_path, "/root/b")

    def test_cursor_bounds_home_end_and_pages(self):
        self.browser.move(-100)
        self.assertEqual(self.browser.cursor, 0)
        self.browser.last()
        self.assertEqual(self.browser.cursor, 3)
        self.browser.move(100)
        self.assertEqual(self.browser.cursor, 3)
        self.browser.first()
        self.browser.page(1, 2)
        self.assertEqual(self.browser.cursor, 2)
        self.browser.page(-1, 50)
        self.assertEqual(self.browser.cursor, 0)


class KeyboardMappingTests(unittest.TestCase):
    def setUp(self):
        self.app = CursesApp.__new__(CursesApp)
        self.app.browser = BrowserController(browser_snapshot())
        self.app.browser.move(2)

    def test_enter_toggles_expansion(self):
        self.assertTrue(self.app.handle_navigation_key(10, 10))
        self.assertIn("/root/b/leaf", self.app.browser.visible_paths)
        self.assertTrue(self.app.handle_navigation_key(10, 10))
        self.assertNotIn("/root/b/leaf", self.app.browser.visible_paths)

    def test_right_and_l_expand_only(self):
        for key in (curses.KEY_RIGHT, ord("l")):
            self.assertTrue(self.app.handle_navigation_key(key, 10))
            self.assertIn("/root/b/leaf", self.app.browser.visible_paths)

    def test_left_and_h_collapse_only(self):
        self.app.browser.expand_selected()
        for key in (curses.KEY_LEFT, ord("h")):
            self.assertTrue(self.app.handle_navigation_key(key, 10))
            self.assertNotIn("/root/b/leaf", self.app.browser.visible_paths)
            self.assertEqual(self.app.browser.selected_path, "/root/b")

    def test_backspace_selects_parent(self):
        self.app.browser.expand_selected()
        self.app.browser.move(1)
        self.assertTrue(self.app.handle_navigation_key(127, 10))
        self.assertEqual(self.app.browser.selected_path, "/root/b")


class ScanControllerTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0

        def successful_scan(path, exclusions, reporter, cancellation, emit):
            self.calls += 1
            cancellation.check()
            emit(record(path, None, "root", 1, 0))
            return {"bytes": 1, "directFilesBytes": 1, "directoryCount": 1}

        self.successful_scan = successful_scan
        self.controller = ScanController(
            ScanPlan("/root", "/root", frozenset()), successful_scan
        )

    def test_rescan_starts_exactly_one_new_core_scan(self):
        self.assertIsNotNone(self.controller.scan().snapshot)
        self.assertEqual(self.calls, 1)
        self.assertIsNotNone(self.controller.scan().snapshot)
        self.assertEqual(self.calls, 2)

    def test_navigation_causes_zero_scans(self):
        snapshot = self.controller.scan().snapshot
        browser = BrowserController(snapshot)
        calls_before = self.calls
        browser.move(1)
        browser.expand_selected()
        browser.collapse_selected()
        browser.select_parent()
        browser.first()
        browser.last()
        self.assertEqual(self.calls, calls_before)

    def test_cancelled_scan_does_not_replace_active_snapshot(self):
        first = self.controller.scan().snapshot

        def cancelled_scan(path, exclusions, reporter, cancellation, emit):
            self.calls += 1
            emit(record(path + "/partial", path, "partial", 5, 0))
            cancellation.cancel()
            cancellation.check()

        self.controller._scan_function = cancelled_scan
        outcome = self.controller.scan()
        self.assertTrue(outcome.cancelled)
        self.assertIsNone(outcome.snapshot)
        self.assertIs(self.controller.active_snapshot, first)
        self.assertEqual(self.calls, 2)


class LauncherTests(unittest.TestCase):
    def test_launcher_help_does_not_require_a_terminal(self):
        process = subprocess.run(
            [str(REPOSITORY / "bin" / "omatree"), "--help"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("usage: omatree", process.stdout)

    def run_launcher(self, home, command):
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["PATH"] = "/nonexistent"
        return subprocess.run(
            [str(REPOSITORY / "bin" / "omatree"), command],
            cwd=REPOSITORY,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_install_and_uninstall_cli_are_explicit_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            destination = home / ".local" / "bin" / "omatree"
            installed = self.run_launcher(home, "install-cli")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.resolve(), (REPOSITORY / "bin" / "omatree").resolve())
            repeated = self.run_launcher(home, "install-cli")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            removed = self.run_launcher(home, "uninstall-cli")
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(os.path.lexists(destination))
            repeated_remove = self.run_launcher(home, "uninstall-cli")
            self.assertEqual(repeated_remove.returncode, 0, repeated_remove.stderr)

    def test_install_refuses_unrelated_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            destination = home / ".local" / "bin" / "omatree"
            destination.parent.mkdir(parents=True)
            destination.write_text("unrelated", encoding="utf-8")
            conflict = self.run_launcher(home, "install-cli")
            self.assertEqual(conflict.returncode, 1)
            self.assertEqual(destination.read_text(encoding="utf-8"), "unrelated")
            destination.unlink()
            destination.symlink_to("/tmp/not-omatree")
            symlink_conflict = self.run_launcher(home, "install-cli")
            self.assertEqual(symlink_conflict.returncode, 1)
            self.assertEqual(os.readlink(destination), "/tmp/not-omatree")

    def test_uninstall_never_removes_unrelated_or_broken_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            destination = home / ".local" / "bin" / "omatree"
            destination.parent.mkdir(parents=True)
            destination.symlink_to(home / "missing-launcher")
            result = self.run_launcher(home, "uninstall-cli")
            self.assertEqual(result.returncode, 1)
            self.assertTrue(os.path.lexists(destination))

    def test_install_and_uninstall_refuse_symlinked_cli_directory(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            home = Path(temp)
            local = home / ".local"
            local.mkdir()
            local.joinpath("bin").symlink_to(outside, target_is_directory=True)
            outside_command = Path(outside) / "omatree"
            outside_command.symlink_to(REPOSITORY / "bin" / "omatree")
            install = self.run_launcher(home, "install-cli")
            self.assertEqual(install.returncode, 1)
            uninstall = self.run_launcher(home, "uninstall-cli")
            self.assertEqual(uninstall.returncode, 1)
            self.assertTrue(outside_command.is_symlink())

    def test_install_rejects_missing_or_relative_home(self):
        for environment in ({}, {"HOME": "relative"}):
            process_environment = os.environ.copy()
            process_environment.pop("HOME", None)
            process_environment.update(environment)
            process = subprocess.run(
                [str(REPOSITORY / "bin" / "omatree"), "install-cli"],
                cwd=REPOSITORY, env=process_environment,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(process.returncode, 1)

    def test_installed_symlink_launches_help_without_ambient_path(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.assertEqual(self.run_launcher(home, "install-cli").returncode, 0)
            command = home / ".local" / "bin" / "omatree"
            environment = os.environ.copy()
            environment.update(HOME=str(home), PATH="/nonexistent")
            process = subprocess.run(
                [str(command), "--help"], env=environment,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("usage: omatree", process.stdout)


if __name__ == "__main__":
    unittest.main()
