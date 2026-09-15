import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


import sys

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "helper"))

from omatree_core.resources import ResourceLimitExceeded, ResourceLimits
from omatree_core.scanner import Cancellation, ScanReporter, scan_tree
from omatree_core.sqlite_snapshot import (
    SQLiteSnapshotError,
    SQLiteSnapshotLimits,
    SQLiteSnapshotValidationError,
    SQLiteSnapshotWriter,
    ensure_snapshot_runtime_dir,
)


def record(path, parent, name=None, bytes_=0, direct=0, children=0, warnings=0):
    return {
        "path": path,
        "parentPath": parent,
        "name": name or Path(path).name or path,
        "bytes": bytes_,
        "directFilesBytes": direct,
        "childDirectoryCount": children,
        "warningCount": warnings,
    }


class SQLiteSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name)
        self.runtime.chmod(0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def writer(self, **kwargs):
        return SQLiteSnapshotWriter.create(self.runtime, **kwargs)

    def valid_snapshot(self):
        writer = self.writer()
        writer.insert_directory(record("/root/b", "/root", "beta", 20, 12))
        writer.insert_directory(record("/root/a", "/root", "Alpha", 20, 8))
        writer.insert_directory(record("/root", None, "root", 40, 4, 2, 1))
        return writer.finalize("/root", 3, {"entries": 9, "allocated_bytes": 40})

    def test_valid_snapshot_and_exact_accounting(self):
        snapshot = self.valid_snapshot()
        try:
            connection = sqlite3.connect(snapshot.path)
            rows = connection.execute("""
                SELECT path, allocated_bytes, direct_files_bytes,
                       child_count, warning_count
                FROM directories ORDER BY path
            """).fetchall()
            metadata = dict(connection.execute(
                "SELECT key, value FROM snapshot_meta"
            ).fetchall())
            connection.close()
            self.assertEqual(rows, [
                ("/root", 40, 4, 2, 1),
                ("/root/a", 20, 8, 0, 0),
                ("/root/b", 20, 12, 0, 0),
            ])
            self.assertEqual(metadata["complete"], "true")
            self.assertEqual(metadata["directory_count"], "3")
            self.assertEqual(snapshot.directory_count, 3)
        finally:
            snapshot.delete()
        self.assertFalse(snapshot.path.exists())

    def test_duplicate_path_rejects_and_deletes_staging_database(self):
        writer = self.writer()
        path = writer.path
        writer.insert_directory(record("/root", None))
        with self.assertRaisesRegex(SQLiteSnapshotError, "duplicate"):
            writer.insert_directory(record("/root", None))
        self.assertFalse(path.exists())

    def assert_validation_failure(self, records, root, count, message):
        writer = self.writer()
        path = writer.path
        for item in records:
            writer.insert_directory(item)
        with self.assertRaisesRegex(SQLiteSnapshotValidationError, message):
            writer.finalize(root, count)
        self.assertFalse(path.exists())

    def test_missing_parent_rejected(self):
        self.assert_validation_failure(
            [record("/root/orphan", "/missing"), record("/root", None)],
            "/root", 2, "no parent",
        )

    def test_child_count_mismatch_rejected(self):
        self.assert_validation_failure(
            [record("/root/child", "/root"), record("/root", None, children=0)],
            "/root", 2, "child count",
        )

    def test_summary_directory_count_mismatch_rejected(self):
        self.assert_validation_failure(
            [record("/root", None)], "/root", 2, "directory count"
        )

    def test_disconnected_hierarchy_rejected(self):
        self.assert_validation_failure(
            [record("/other", None), record("/root", None)],
            "/root", 2, "null-parent",
        )

    def test_cycle_rejected_without_loading_tree(self):
        self.assert_validation_failure(
            [
                record("/cycle/a", "/cycle/b", children=1),
                record("/cycle/b", "/cycle/a", children=1),
                record("/root", None),
            ],
            "/root", 3, "cyclic or disconnected",
        )

    def test_context_exit_rolls_back_and_deletes_staging_database(self):
        with self.writer() as writer:
            path = writer.path
            writer.insert_directory(record("/root", None))
        self.assertFalse(path.exists())

    def test_close_rolls_back_uncommitted_rows(self):
        writer = self.writer()
        path = writer.path
        writer.insert_directory(record("/root", None))
        writer.close()
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM directories").fetchone()[0],
                0,
            )
        finally:
            connection.close()
            path.unlink()

    def test_database_size_ceiling_fails_and_cleans_up(self):
        limits = SQLiteSnapshotLimits(max_database_bytes=32 * 1024)
        writer = self.writer(sqlite_limits=limits)
        path = writer.path
        with self.assertRaisesRegex(ResourceLimitExceeded, "SQLite snapshot bytes"):
            for index in range(200):
                child = f"/root/{index:04d}-" + "x" * 500
                writer.insert_directory(record(child, "/root"))
        self.assertFalse(path.exists())

    def test_private_random_temporary_path(self):
        first = self.writer()
        second = self.writer()
        try:
            self.assertEqual(first.path.parent, self.runtime / "omatree")
            self.assertNotEqual(first.path.name, second.path.name)
            self.assertTrue(first.path.name.startswith("generation-"))
            self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.runtime / "omatree").stat().st_mode & 0o777, 0o700)
        finally:
            first.abort()
            second.abort()

    def test_symlinked_runtime_paths_are_rejected(self):
        real = self.runtime / "real"
        real.mkdir(mode=0o700)
        link = self.runtime / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(SQLiteSnapshotError, "symlink"):
            ensure_snapshot_runtime_dir(link)

        unsafe_target = self.runtime / "outside"
        unsafe_target.mkdir(mode=0o700)
        (real / "omatree").symlink_to(unsafe_target, target_is_directory=True)
        with self.assertRaisesRegex(SQLiteSnapshotError, "symlink"):
            ensure_snapshot_runtime_dir(real)

    def test_snapshot_file_cannot_escape_runtime_root(self):
        runtime_root = ensure_snapshot_runtime_dir(self.runtime)
        outside = self.runtime / "outside.sqlite"
        descriptor = os.open(outside, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        with mock.patch(
            "omatree_core.sqlite_snapshot.tempfile.mkstemp",
            return_value=(descriptor, str(outside)),
        ):
            with self.assertRaisesRegex(SQLiteSnapshotError, "escaped"):
                SQLiteSnapshotWriter.create(self.runtime)
        self.assertTrue(outside.exists())
        self.assertEqual(runtime_root, self.runtime / "omatree")

    def test_indexes_support_deterministic_child_order(self):
        snapshot = self.valid_snapshot()
        try:
            connection = sqlite3.connect(snapshot.path)
            indexes = {
                row[1] for row in connection.execute("PRAGMA index_list(directories)")
            }
            children = connection.execute("""
                SELECT path FROM directories WHERE parent_path = ?
                ORDER BY allocated_bytes DESC, name_fold, path
            """, ("/root",)).fetchall()
            connection.close()
            self.assertIn("directories_parent_order", indexes)
            self.assertIn("directories_name_fold", indexes)
            self.assertEqual(children, [("/root/a",), ("/root/b",)])
        finally:
            snapshot.delete()

    def test_sqlite_pragmas_are_explicit_and_bounded(self):
        writer = self.writer()
        try:
            connection = writer._connection
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(connection.execute("PRAGMA cache_size").fetchone()[0], -16384)
            self.assertEqual(connection.execute("PRAGMA temp_store").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA trusted_schema").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 2000)
            self.assertEqual(
                connection.execute("PRAGMA max_page_count").fetchone()[0],
                (2 * 1024 * 1024 * 1024) // 4096,
            )
            with self.assertRaisesRegex(sqlite3.OperationalError, "not authorized"):
                connection.load_extension("not-a-real-extension")
        finally:
            writer.abort()

    def test_scanner_records_insert_incrementally_without_protocol_changes(self):
        scan_root = self.runtime / "scan-root"
        child = scan_root / "child"
        child.mkdir(parents=True)
        (child / "data").write_bytes(b"x" * 4096)
        writer = self.writer()
        reporter = ScanReporter("sqlite-test", lambda _event: None)
        summary = scan_tree(
            str(scan_root), set(), reporter, Cancellation(),
            writer.insert_directory,
        )
        snapshot = writer.finalize(str(scan_root), summary["directoryCount"])
        try:
            connection = sqlite3.connect(snapshot.path)
            stored = connection.execute("""
                SELECT allocated_bytes, direct_files_bytes, child_count
                FROM directories WHERE path = ?
            """, (str(scan_root),)).fetchone()
            connection.close()
            self.assertEqual(stored[0], summary["bytes"])
            self.assertEqual(stored[1], summary["directFilesBytes"])
            self.assertEqual(stored[2], 1)
        finally:
            snapshot.delete()

    def test_large_synthetic_snapshot_does_not_build_python_directory_graph(self):
        count = 10_000
        resource_limits = ResourceLimits(max_directories=count + 1)
        writer = self.writer(resource_limits=resource_limits)
        for index in range(count):
            writer.insert_directory(record(f"/root/d{index:05d}", "/root"))
        writer.insert_directory(record("/root", None, children=count))
        self.assertFalse(any(
            isinstance(value, (list, dict, set))
            for value in writer.__dict__.values()
        ))
        snapshot = writer.finalize("/root", count + 1)
        try:
            connection = sqlite3.connect(snapshot.path)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM directories").fetchone()[0],
                count + 1,
            )
            connection.close()
        finally:
            snapshot.delete()


if __name__ == "__main__":
    unittest.main()
