import json
from pathlib import Path
import sys
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "helper"))

from omatree_core import protocol
from omatree_core.snapshot import SnapshotBuilder, SnapshotError, snapshot_from_events


FIXTURES = REPOSITORY / "tests" / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ProtocolTests(unittest.TestCase):
    def test_protocol_constants_and_compact_ndjson(self):
        self.assertEqual(protocol.PROTOCOL_VERSION, 1)
        self.assertEqual(
            protocol.EVENT_TYPES,
            {"start", "progress", "warning", "directory", "complete", "cancelled", "error"},
        )
        event = protocol.progress_event("request", 3, 4096)
        self.assertEqual(
            protocol.encode_ndjson(event),
            '{"type":"progress","requestId":"request","entries":3,"bytes":4096}\n',
        )

    def test_event_helpers_preserve_existing_field_shapes(self):
        self.assertEqual(
            list(protocol.start_event("r", "/p", "/m", ["/m/n"])),
            ["type", "requestId", "path", "mountpoint", "excludedMountpoints"],
        )
        self.assertEqual(
            list(protocol.directory_event("r", {
                "path": "/p", "parentPath": None, "name": "p", "bytes": 1,
                "directFilesBytes": 1, "childDirectoryCount": 0, "warningCount": 0,
            })),
            ["type", "requestId", "path", "parentPath", "name", "bytes",
             "directFilesBytes", "childDirectoryCount", "warningCount"],
        )
        self.assertNotIn("protocolVersion", protocol.start_event("r", "/p", "/m", []))


class SnapshotFixtureTests(unittest.TestCase):
    def assert_fixture_error(self, name, message):
        with self.assertRaisesRegex(SnapshotError, message):
            snapshot_from_events(load_fixture(name))

    def test_valid_completed_scan_builds_directory_only_snapshot(self):
        snapshot = snapshot_from_events(load_fixture("valid_completed_scan.json"))
        self.assertEqual(len(snapshot.directories), 4)
        self.assertEqual(snapshot.root.children, ["/root/alpha", "/root/beta"])
        self.assertEqual(snapshot.directories["/root/alpha"].depth, 1)
        self.assertEqual(snapshot.directories["/root/alpha/leaf"].depth, 2)
        self.assertFalse(hasattr(snapshot.root, "expanded"))

    def test_cancelled_partial_stream_never_commits(self):
        self.assert_fixture_error(
            "cancelled_scan.json", "did not complete successfully"
        )

    def test_malformed_stream_is_rejected(self):
        self.assert_fixture_error("malformed_stream.json", "Malformed scan event")

    def test_duplicate_directory_is_rejected(self):
        self.assert_fixture_error(
            "duplicate_directory.json", "duplicate or empty directory paths"
        )

    def test_missing_parent_is_rejected(self):
        self.assert_fixture_error("missing_parent.json", "no parent")

    def test_child_count_mismatch_is_rejected(self):
        self.assert_fixture_error(
            "child_count_mismatch.json", "inconsistent directory hierarchy"
        )

    def test_equal_size_children_use_case_insensitive_name_order(self):
        snapshot = snapshot_from_events(load_fixture("equal_size_sorting.json"))
        self.assertEqual(
            snapshot.root.children,
            ["/root/middle", "/root/Alpha", "/root/zulu"],
        )

    def test_zero_byte_percentages_match_tree_model(self):
        snapshot = snapshot_from_events(load_fixture("zero_byte_percentages.json"))
        self.assertEqual(snapshot.percentage_of_parent("/zero"), 0)
        self.assertEqual(snapshot.percentage_of_parent("/zero/child"), 0)
        self.assertEqual(snapshot.percentage_of_parent("/missing"), 0)

    def test_ancestors_and_nonzero_percentages_match_tree_model(self):
        snapshot = snapshot_from_events(load_fixture("valid_completed_scan.json"))
        self.assertEqual(snapshot.percentage_of_parent("/root"), 100)
        self.assertEqual(snapshot.percentage_of_parent("/root/alpha"), 40)
        self.assertEqual(
            snapshot.ancestor_paths("/root/alpha/leaf"),
            ["/root", "/root/alpha", "/root/alpha/leaf"],
        )

    def test_search_ranking_total_and_limit_match_tree_model(self):
        snapshot = snapshot_from_events(load_fixture("search_ordering.json"))
        result = snapshot.search("DATA", 2)
        self.assertEqual(result.total, 3)
        self.assertEqual(
            [node.path for node in result.matches],
            ["/root/a-data", "/root/z-data"],
        )
        self.assertEqual(snapshot.search("", 2).total, 0)


class SnapshotValidationTests(unittest.TestCase):
    def test_cycle_is_rejected(self):
        builder = SnapshotBuilder()
        builder.add_directory({
            "path": "/root", "parentPath": "/root/child", "name": "root",
            "bytes": 1, "directFilesBytes": 0, "childDirectoryCount": 1,
            "warningCount": 0,
        })
        builder.add_directory({
            "path": "/root/child", "parentPath": "/root", "name": "child",
            "bytes": 1, "directFilesBytes": 1, "childDirectoryCount": 1,
            "warningCount": 0,
        })
        with self.assertRaisesRegex(SnapshotError, "cyclic"):
            builder.finalize("/root", 2)

    def test_disconnected_directory_is_rejected(self):
        builder = SnapshotBuilder()
        builder.add_directory({
            "path": "/root", "parentPath": None, "name": "root", "bytes": 1,
            "directFilesBytes": 1, "childDirectoryCount": 0, "warningCount": 0,
        })
        builder.add_directory({
            "path": "/other", "parentPath": None, "name": "other", "bytes": 1,
            "directFilesBytes": 1, "childDirectoryCount": 0, "warningCount": 0,
        })
        with self.assertRaisesRegex(SnapshotError, "outside the filesystem root"):
            builder.finalize("/root", 2)

    def test_missing_root_is_rejected(self):
        with self.assertRaisesRegex(SnapshotError, "root directory"):
            SnapshotBuilder().finalize("/root", 0)


if __name__ == "__main__":
    unittest.main()
