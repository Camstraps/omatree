import json
import os
import base64
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import sys

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "helper"))

from omatree_core.sqlite_query import (
    DEFAULT_QUERY_LIMITS,
    DirectoryRow,
    InvalidContinuationToken,
    QueryLimits,
    QueryPage,
    SQLiteQueryError,
    SQLiteSnapshotReader,
    SnapshotCorruptionError,
    encode_query_page,
)
from omatree_core.sqlite_snapshot import SQLiteSnapshotWriter


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


class SQLiteQueryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name)
        self.runtime.chmod(0o700)
        self.snapshots = []

    def tearDown(self):
        for snapshot in self.snapshots:
            snapshot.delete()
        self.temporary.cleanup()

    def snapshot(self, records, root="/root"):
        writer = SQLiteSnapshotWriter.create(self.runtime)
        for item in records:
            writer.insert_directory(item)
        snapshot = writer.finalize(root, len(records))
        self.snapshots.append(snapshot)
        return snapshot

    def snapshot_with_files(self, records, files, root="/root"):
        writer = SQLiteSnapshotWriter.create(self.runtime)
        for item in files:
            writer.insert_file(item)
        for item in records:
            writer.insert_directory(item)
        snapshot = writer.finalize(root, len(records))
        self.snapshots.append(snapshot)
        return snapshot

    def basic_snapshot(self):
        return self.snapshot([
            record("/root/b", "/root", "beta", 20, 12, warnings=1),
            record("/root/a", "/root", "Alpha", 20, 8),
            record("/root/c", "/root", "charlie", 10, 3),
            record("/root", None, "root", 50, 4, 3, 2),
        ])

    def mutate(self, snapshot, statements):
        connection = sqlite3.connect(snapshot.path)
        try:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            for sql, parameters in statements:
                connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def test_completed_snapshot_opens_read_only_with_bounded_pragmas(self):
        snapshot = self.basic_snapshot()
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            connection = reader._connection
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA trusted_schema").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA cache_size").fetchone()[0], -16384)
            self.assertEqual(connection.execute("PRAGMA temp_store").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 2000)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM directories")
            with self.assertRaisesRegex(sqlite3.OperationalError, "not authorized"):
                connection.load_extension("not-a-real-extension")

    def test_metadata_exact_unknown_and_input_bounds(self):
        snapshot = self.basic_snapshot()
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            row = reader.metadata("/root/b")
            self.assertEqual(row, DirectoryRow("/root/b", "/root", "beta", 20, 12, 0, 1))
            self.assertIsNone(reader.metadata("/root/missing"))
            with self.assertRaisesRegex(SQLiteQueryError, "nonempty"):
                reader.metadata("")
            with self.assertRaisesRegex(SQLiteQueryError, "exceeds"):
                reader.metadata("/" + "x" * 4096)

    def test_children_use_canonical_order_and_keyset_pages(self):
        snapshot = self.basic_snapshot()
        limits = QueryLimits(max_rows=2)
        with SQLiteSnapshotReader.open(snapshot.path, query_limits=limits) as reader:
            first = reader.children("/root")
            second = reader.children("/root", first.continuation)
            self.assertEqual([row.path for row in first.rows], ["/root/a", "/root/b"])
            self.assertTrue(first.has_more)
            self.assertEqual([row.path for row in second.rows], ["/root/c"])
            self.assertFalse(second.has_more)
            self.assertIsNone(second.continuation)
            self.assertEqual(reader.children("/root/c").rows, ())
            self.assertEqual(reader.children("/missing").rows, ())

    def test_children_page_directories_before_files_and_remain_bounded(self):
        files = [{"path": f"/root/f{i:04d}", "parentPath": "/root",
                  "name": f"f{i:04d}", "bytes": 10000 - i} for i in range(1000)]
        root_record = record("/root", None, children=1)
        root_record["fileCount"] = 1000
        snapshot = self.snapshot_with_files([
            record("/root/d", "/root", "d", 1), root_record,
        ], files)
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            first = reader.children("/root")
            self.assertEqual(reader.metadata("/root").child_count, 1001)
            self.assertEqual(first.rows[0].kind, "directory")
            self.assertTrue(all(row.kind == "file" for row in first.rows[1:]))
            self.assertLessEqual(len(first.rows), 64)
            visited = len(first.rows)
            token = first.continuation
            while token:
                page = reader.children("/root", token)
                self.assertLessEqual(len(page.rows), 64)
                visited += len(page.rows)
                token = page.continuation
            self.assertEqual(visited, 1001)

    def test_children_at_reveals_late_sibling_without_offset_or_prior_pages(self):
        count = 10_000
        records = [
            record(f"/root/d{index:05d}", "/root", f"d{index:05d}", count - index)
            for index in range(count)
        ]
        records.append(record("/root", None, children=count))
        snapshot = self.snapshot(records)
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            with mock.patch("omatree_core.scanner.os.scandir", side_effect=AssertionError):
                page = reader.children_at("/root/d09000")
            self.assertEqual(page.kind, "childrenAt")
            self.assertEqual(page.rows[0].path, "/root/d09000")
            self.assertLessEqual(len(page.rows), 64)
            self.assertNotIn("OFFSET", SQLiteSnapshotReader.children_at.__doc__ or "")
            if page.has_more:
                following = reader.children("/root", page.continuation)
                self.assertGreater(following.rows[0].path, page.rows[-1].path)

    def test_continuations_reject_malformed_stale_and_wrong_scope(self):
        first_snapshot = self.basic_snapshot()
        second_snapshot = self.snapshot([record("/other", None)], "/other")
        limits = QueryLimits(max_rows=1)
        with SQLiteSnapshotReader.open(first_snapshot.path, query_limits=limits) as first:
            token = first.children("/root").continuation
            with self.assertRaises(InvalidContinuationToken):
                first.children("/root", "not-base64!")
            with self.assertRaises(InvalidContinuationToken):
                first.children("/other", token)
            with SQLiteSnapshotReader.open(second_snapshot.path, query_limits=limits) as second:
                with self.assertRaises(InvalidContinuationToken):
                    second.children("/root", token)

            forged_payload = {
                "v": 1, "kind": "children", "snapshot": first.snapshot_id,
                "scope": "/root", "key": [999, "invented", "/root/invented"],
            }
            forged = base64.urlsafe_b64encode(json.dumps(
                forged_payload, separators=(",", ":")
            ).encode()).decode().rstrip("=")
            with self.assertRaisesRegex(InvalidContinuationToken, "not in snapshot"):
                first.children("/root", forged)

    def test_huge_flat_parent_pages_without_result_history(self):
        count = 2_000
        records = [
            record(f"/root/d{index:05d}", "/root", f"d{index:05d}", count - index)
            for index in range(count)
        ]
        records.append(record("/root", None, children=count))
        snapshot = self.snapshot(records)
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            token = None
            seen = []
            with mock.patch("omatree_core.scanner.os.scandir", side_effect=AssertionError):
                while True:
                    page = reader.children("/root", token)
                    self.assertLessEqual(len(page.rows), 64)
                    self.assertLessEqual(
                        len(encode_query_page(page).encode()),
                        DEFAULT_QUERY_LIMITS.max_response_bytes,
                    )
                    seen.extend(row.path for row in page.rows)
                    if not page.has_more:
                        break
                    token = page.continuation
            self.assertEqual(len(seen), count)
            self.assertEqual(len(set(seen)), count)
            self.assertFalse(any(
                isinstance(value, (list, dict, set))
                for value in reader.__dict__.values()
            ))

    def test_search_is_complete_casefolded_largest_first_and_bounded(self):
        count = 1_000
        records = [
            record(
                f"/root/m{index:04d}", "/root", f"MaTcH-{index:04d}",
                (index % 17) * 100,
            )
            for index in range(count)
        ]
        records.append(record("/root", None, children=count))
        snapshot = self.snapshot(records)
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            token = None
            rows = []
            while True:
                page = reader.search("match", token)
                self.assertLessEqual(len(page.rows), 64)
                rows.extend(page.rows)
                if not page.has_more:
                    break
                token = page.continuation
            self.assertEqual(len(rows), count)
            keys = [(-row.allocated_bytes, row.name.casefold(), row.path) for row in rows]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(reader.search("no-hit").rows, ())
            with self.assertRaisesRegex(SQLiteQueryError, "byte limit"):
                reader.search("x" * 1025)

    def test_search_token_is_bound_to_query(self):
        snapshot = self.snapshot([
            record("/root/a", "/root", "match-a", 2),
            record("/root/b", "/root", "match-b", 1),
            record("/root", None, children=2),
        ])
        with SQLiteSnapshotReader.open(
            snapshot.path, query_limits=QueryLimits(max_rows=1)
        ) as reader:
            token = reader.search("match").continuation
            with self.assertRaises(InvalidContinuationToken):
                reader.search("other", token)

    def test_deep_ancestor_chain_is_paginated_without_duplicates(self):
        depth = 130
        paths = ["/root"]
        for index in range(depth):
            paths.append(paths[-1] + f"/d{index}")
        records = []
        for index in range(len(paths) - 1, -1, -1):
            parent = None if index == 0 else paths[index - 1]
            records.append(record(
                paths[index], parent, children=0 if index == depth else 1
            ))
        snapshot = self.snapshot(records)
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            token = None
            found = []
            while True:
                page = reader.ancestors(paths[-1], token)
                self.assertLessEqual(len(page.rows), 64)
                found.extend(row.path for row in page.rows)
                if not page.has_more:
                    break
                token = page.continuation
            self.assertEqual(found, list(reversed(paths)))

    def test_long_legal_rows_are_adaptively_packed(self):
        root = "/" + "r" * 4095
        records = []
        for index in range(70):
            suffix = f"{index:04d}"
            path = "/" + suffix + "x" * 4091
            name = "n" * 4092 + suffix
            records.append(record(path, root, name, 70 - index))
        records.append(record(root, None, "root", children=70))
        snapshot = self.snapshot(records, root)
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            page = reader.children(root)
            encoded = encode_query_page(page).encode()
            self.assertLess(len(page.rows), 64)
            self.assertGreater(len(page.rows), 0)
            self.assertTrue(page.has_more)
            self.assertLessEqual(len(encoded), DEFAULT_QUERY_LIMITS.row_payload_budget)
            next_page = reader.children(root, page.continuation)
            self.assertNotEqual(page.rows[-1].path, next_page.rows[0].path)

    def test_encoder_rejects_malformed_values_and_hard_overflow(self):
        malformed = DirectoryRow("/x", None, "x", -1, 0, 0, 0)
        with self.assertRaises(SnapshotCorruptionError):
            encode_query_page(QueryPage("metadata", (malformed,), False, None))
        long_row = DirectoryRow("/" + "x" * 100, None, "x", 0, 0, 0, 0)
        with self.assertRaisesRegex(SQLiteQueryError, "hard limit"):
            encode_query_page(
                QueryPage("test", (long_row,), False, None),
                QueryLimits(max_response_bytes=64, row_payload_budget=64),
            )

    def test_incomplete_invalid_schema_and_metadata_count_are_rejected(self):
        staging = SQLiteSnapshotWriter.create(self.runtime)
        staging.insert_directory(record("/root", None))
        staging._connection.commit()
        with self.assertRaises((SnapshotCorruptionError, SQLiteQueryError)):
            SQLiteSnapshotReader.open(staging.path)
        staging.abort()

        invalid = self.runtime / "invalid.sqlite"
        connection = sqlite3.connect(invalid)
        connection.execute("CREATE TABLE unrelated(value)")
        connection.close()
        invalid.chmod(0o600)
        with self.assertRaises(SnapshotCorruptionError):
            SQLiteSnapshotReader.open(invalid)

        snapshot = self.basic_snapshot()
        self.mutate(snapshot, [
            ("UPDATE snapshot_meta SET value = '99' WHERE key = 'directory_count'", ()),
        ])
        with self.assertRaisesRegex(SnapshotCorruptionError, "inconsistent"):
            SQLiteSnapshotReader.open(snapshot.path)

    def test_unsafe_snapshot_file_is_rejected(self):
        snapshot = self.basic_snapshot()
        snapshot.path.chmod(0o644)
        with self.assertRaisesRegex(SQLiteQueryError, "unsafe permissions"):
            SQLiteSnapshotReader.open(snapshot.path)
        snapshot.path.chmod(0o600)

    def test_oversized_and_malformed_rows_fail_before_partial_page(self):
        snapshot = self.basic_snapshot()
        self.mutate(snapshot, [
            ("UPDATE directories SET name = ? WHERE path = '/root/c'", ("x" * 4097,)),
        ])
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            with self.assertRaisesRegex(SnapshotCorruptionError, "exceeds"):
                reader.children("/root")

        for value in (-1, 1.5, "bad"):
            snapshot = self.basic_snapshot()
            self.mutate(snapshot, [
                ("UPDATE directories SET allocated_bytes = ? WHERE path = '/root/a'", (value,)),
            ])
            with SQLiteSnapshotReader.open(snapshot.path) as reader:
                with self.assertRaisesRegex(SnapshotCorruptionError, "accounting"):
                    reader.children("/root")

    def test_corrupt_name_fold_is_rejected(self):
        snapshot = self.basic_snapshot()
        self.mutate(snapshot, [
            ("UPDATE directories SET name_fold = 'wrong' WHERE path = '/root/a'", ()),
        ])
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            with self.assertRaisesRegex(SnapshotCorruptionError, "search name"):
                reader.children("/root")

    def test_missing_parent_cycle_and_null_parent_fail_ancestor_query(self):
        snapshot = self.basic_snapshot()
        self.mutate(snapshot, [
            ("UPDATE directories SET parent_path = '/missing' WHERE path = '/root/a'", ()),
        ])
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            with self.assertRaisesRegex(SnapshotCorruptionError, "missing a parent"):
                reader.ancestors("/root/a")

        snapshot = self.basic_snapshot()
        self.mutate(snapshot, [
            ("UPDATE directories SET parent_path = '/root/b' WHERE path = '/root/a'", ()),
            ("UPDATE directories SET parent_path = '/root/a' WHERE path = '/root/b'", ()),
        ])
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            with self.assertRaisesRegex(SnapshotCorruptionError, "cyclic"):
                reader.ancestors("/root/a")

        snapshot = self.basic_snapshot()
        self.mutate(snapshot, [
            ("UPDATE directories SET parent_path = NULL WHERE path = '/root/a'", ()),
        ])
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            with self.assertRaisesRegex(SnapshotCorruptionError, "disconnected"):
                reader.ancestors("/root/a")

    def test_database_change_between_pages_invalidates_reader(self):
        snapshot = self.basic_snapshot()
        with SQLiteSnapshotReader.open(
            snapshot.path, query_limits=QueryLimits(max_rows=1)
        ) as reader:
            page = reader.children("/root")
            self.mutate(snapshot, [
                ("UPDATE directories SET warning_count = 9 WHERE path = '/root/c'", ()),
            ])
            with self.assertRaisesRegex(SQLiteQueryError, "changed"):
                reader.children("/root", page.continuation)

    def test_repeated_pages_do_not_accumulate_reader_state(self):
        snapshot = self.basic_snapshot()
        with SQLiteSnapshotReader.open(
            snapshot.path, query_limits=QueryLimits(max_rows=1)
        ) as reader:
            initial_keys = set(reader.__dict__)
            for _ in range(100):
                first = reader.children("/root")
                reader.children("/root", first.continuation)
                first = reader.search("a")
                if first.has_more:
                    reader.search("a", first.continuation)
            self.assertEqual(set(reader.__dict__), initial_keys)
            self.assertFalse(any(
                isinstance(value, (list, dict, set))
                for value in reader.__dict__.values()
            ))

    def test_source_uses_no_unbounded_fetchall(self):
        source = (REPOSITORY / "helper/omatree_core/sqlite_query.py").read_text()
        self.assertNotIn(".fetchall(", source)


if __name__ == "__main__":
    unittest.main()
