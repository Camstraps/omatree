from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from helper.omatree_core import scanner
from helper.omatree_core.snapshot import SnapshotBuilder
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
        self.browser.collapse_or_parent()
        self.assertNotIn("/root/b/leaf", self.browser.visible_paths)
        self.browser.collapse_or_parent()
        self.assertEqual(self.browser.selected_path, "/root")

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
        browser.collapse_or_parent()
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


if __name__ == "__main__":
    unittest.main()
