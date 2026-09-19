"""Strictly bounded, read-only queries over completed SQLite snapshots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Callable, Iterable
from urllib.parse import quote

from .resources import (
    DEFAULT_LIMITS, ResourceLimitExceeded, ResourceLimits, check_path,
)
from .sqlite_snapshot import (
    DEFAULT_SQLITE_LIMITS,
    SQLITE_SNAPSHOT_SCHEMA_VERSION,
    SQLiteSnapshotLimits,
)

MAX_QUERY_ROWS = 64
MAX_QUERY_RESPONSE_BYTES = 1024 * 1024
MAX_QUERY_PAYLOAD_BYTES = 768 * 1024
MAX_QUERY_STRING_BYTES = 4096
MAX_SEARCH_QUERY_BYTES = 1024
MAX_BREADCRUMB_ROWS = 64
MAX_SQLITE_VALUE_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class QueryLimits:
    max_rows: int = MAX_QUERY_ROWS
    max_response_bytes: int = MAX_QUERY_RESPONSE_BYTES
    row_payload_budget: int = MAX_QUERY_PAYLOAD_BYTES
    max_string_bytes: int = MAX_QUERY_STRING_BYTES
    max_search_query_bytes: int = MAX_SEARCH_QUERY_BYTES
    max_breadcrumb_rows: int = MAX_BREADCRUMB_ROWS

    def __post_init__(self) -> None:
        values = (
            self.max_rows, self.max_response_bytes, self.row_payload_budget,
            self.max_string_bytes, self.max_search_query_bytes,
            self.max_breadcrumb_rows,
        )
        if any(value <= 0 for value in values):
            raise ValueError("query limits must be positive")
        ceilings = (
            (self.max_rows, MAX_QUERY_ROWS),
            (self.max_response_bytes, MAX_QUERY_RESPONSE_BYTES),
            (self.row_payload_budget, MAX_QUERY_PAYLOAD_BYTES),
            (self.max_string_bytes, MAX_QUERY_STRING_BYTES),
            (self.max_search_query_bytes, MAX_SEARCH_QUERY_BYTES),
            (self.max_breadcrumb_rows, MAX_BREADCRUMB_ROWS),
        )
        if any(value > ceiling for value, ceiling in ceilings):
            raise ValueError("query limits may not exceed security ceilings")
        if self.row_payload_budget > self.max_response_bytes:
            raise ValueError("row payload budget exceeds response limit")


DEFAULT_QUERY_LIMITS = QueryLimits()


class SQLiteQueryError(RuntimeError):
    """A snapshot cannot safely answer a read request."""


class InvalidContinuationToken(SQLiteQueryError):
    """A continuation token is malformed, stale, or for another query."""


class SnapshotCorruptionError(SQLiteQueryError):
    """A completed snapshot or returned row violates its contract."""


@dataclass(frozen=True, slots=True)
class DirectoryRow:
    path: str
    parent_path: str | None
    name: str
    allocated_bytes: int
    direct_files_bytes: int
    child_count: int
    warning_count: int
    kind: str = "directory"


@dataclass(frozen=True, slots=True)
class QueryPage:
    kind: str
    rows: tuple[DirectoryRow, ...]
    has_more: bool
    continuation: str | None


_EXPECTED_COLUMNS = (
    ("path", "TEXT"),
    ("parent_path", "TEXT"),
    ("name", "TEXT"),
    ("name_fold", "TEXT"),
    ("allocated_bytes", "INTEGER"),
    ("direct_files_bytes", "INTEGER"),
    ("child_count", "INTEGER"),
    ("file_count", "INTEGER"),
    ("warning_count", "INTEGER"),
    ("depth", "INTEGER"),
)

_EXPECTED_FILE_COLUMNS = (
    ("path", "TEXT"), ("parent_path", "TEXT"), ("name", "TEXT"),
    ("name_fold", "TEXT"), ("allocated_bytes", "INTEGER"),
)

_ROW_SELECT = """path, parent_path, name, allocated_bytes,
                  direct_files_bytes, (child_count + file_count), warning_count"""


def _encoded_size(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="surrogateescape"))
    except UnicodeEncodeError as error:
        raise SQLiteQueryError("string is not safely encodable") from error


def _token_encode(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _token_decode(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 32 * 1024:
        raise InvalidContinuationToken("invalid continuation token")
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.b64decode(
            token + padding, altchars=b"-_", validate=True
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidContinuationToken("invalid continuation token") from error
    if not isinstance(payload, dict):
        raise InvalidContinuationToken("invalid continuation token")
    return payload


def encode_query_page(
    page: QueryPage, limits: QueryLimits = DEFAULT_QUERY_LIMITS,
) -> str:
    """Encode one already bounded page and enforce the hard response ceiling."""
    if len(page.rows) > limits.max_rows:
        raise SQLiteQueryError("query page exceeds row limit")
    if (
        not isinstance(page.kind, str) or not page.kind
        or _encoded_size(page.kind) > 64 or not isinstance(page.has_more, bool)
        or (page.has_more and not isinstance(page.continuation, str))
        or (not page.has_more and page.continuation is not None)
        or (
            page.continuation is not None
            and _encoded_size(page.continuation) > 32 * 1024
        )
    ):
        raise SQLiteQueryError("query response envelope is malformed")
    for row in page.rows:
        if not isinstance(row, DirectoryRow):
            raise SnapshotCorruptionError("query response row is malformed")
        strings = (row.path, row.name)
        if row.parent_path is not None:
            strings += (row.parent_path,)
        if any(not isinstance(value, str) or not value for value in strings):
            raise SnapshotCorruptionError("query response strings are malformed")
        if any(_encoded_size(value) > limits.max_string_bytes for value in strings):
            raise SnapshotCorruptionError("query response string exceeds limit")
        numbers = (
            row.allocated_bytes, row.direct_files_bytes,
            row.child_count, row.warning_count,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in numbers
        ):
            raise SnapshotCorruptionError("query response accounting is malformed")
    payload = {
        "kind": page.kind,
        "rows": [asdict(row) for row in page.rows],
        "hasMore": page.has_more,
        "continuation": page.continuation,
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
    if len(encoded.encode("utf-8")) > limits.max_response_bytes:
        raise SQLiteQueryError("encoded query response exceeds hard limit")
    return encoded


class SQLiteSnapshotReader:
    """Read bounded pages from one immutable, successfully completed snapshot."""

    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        query_limits: QueryLimits,
        resource_limits: ResourceLimits,
        sqlite_limits: SQLiteSnapshotLimits,
    ) -> None:
        self.path = path
        self._connection = connection
        self.query_limits = query_limits
        self.resource_limits = resource_limits
        self.sqlite_limits = sqlite_limits
        self.snapshot_id = ""
        self.root_path = ""
        self.directory_count = 0
        self.file_count = 0
        self._identity = (0, 0)
        self._mtime_ns = 0
        self._size = 0
        self._data_version = 0
        self._closed = False

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        query_limits: QueryLimits = DEFAULT_QUERY_LIMITS,
        resource_limits: ResourceLimits = DEFAULT_LIMITS,
        sqlite_limits: SQLiteSnapshotLimits = DEFAULT_SQLITE_LIMITS,
    ) -> "SQLiteSnapshotReader":
        snapshot_path = Path(os.path.abspath(os.fspath(path)))
        try:
            details = os.lstat(snapshot_path)
        except OSError as error:
            raise SQLiteQueryError("snapshot database is unavailable") from error
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or details.st_size > sqlite_limits.max_database_bytes
        ):
            raise SQLiteQueryError("snapshot database has unsafe permissions")
        uri = "file:" + quote(str(snapshot_path), safe="/") + "?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                uri, uri=True, isolation_level=None,
                timeout=sqlite_limits.busy_timeout_ms / 1000,
            )
            reader = cls(
                snapshot_path, connection, query_limits,
                resource_limits, sqlite_limits,
            )
            reader._configure()
            reader._validate_completed_snapshot()
            current = os.stat(snapshot_path, follow_symlinks=False)
            reader._identity = (current.st_dev, current.st_ino)
            reader._mtime_ns = current.st_mtime_ns
            reader._size = current.st_size
            reader._data_version = int(
                connection.execute("PRAGMA data_version").fetchone()[0]
            )
            return reader
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise SnapshotCorruptionError("snapshot database is invalid") from error
        except BaseException:
            try:
                if connection is not None:
                    connection.close()
            except sqlite3.Error:
                pass
            raise

    def _configure(self) -> None:
        connection = self._connection
        connection.enable_load_extension(False)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQLITE_VALUE_BYTES)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute(f"PRAGMA cache_size = -{self.sqlite_limits.cache_kib}")
        connection.execute("PRAGMA temp_store = FILE")
        connection.execute(f"PRAGMA busy_timeout = {self.sqlite_limits.busy_timeout_ms}")

    def _bounded_rows(
        self, cursor: sqlite3.Cursor, maximum: int,
    ) -> list[tuple[Any, ...]]:
        try:
            rows = cursor.fetchmany(maximum + 1)
        except sqlite3.Error as error:
            raise SnapshotCorruptionError("snapshot row exceeds SQLite limits") from error
        if len(rows) > maximum:
            raise SnapshotCorruptionError("schema query exceeded expected size")
        return rows

    @staticmethod
    def _fetchmany(cursor: sqlite3.Cursor, maximum: int) -> list[tuple[Any, ...]]:
        try:
            return cursor.fetchmany(maximum)
        except sqlite3.Error as error:
            raise SnapshotCorruptionError("snapshot row exceeds SQLite limits") from error

    @staticmethod
    def _fetchone(cursor: sqlite3.Cursor) -> tuple[Any, ...] | None:
        try:
            return cursor.fetchone()
        except sqlite3.Error as error:
            raise SnapshotCorruptionError("snapshot row exceeds SQLite limits") from error

    def _metadata_value(self, key: str) -> Any:
        size_row = self._fetchone(self._connection.execute(
            "SELECT length(CAST(value AS BLOB)) FROM snapshot_meta WHERE key = ?",
            (key,),
        ))
        if (
            size_row is None or isinstance(size_row[0], bool)
            or not isinstance(size_row[0], int)
            or size_row[0] > self.sqlite_limits.max_metadata_bytes
        ):
            raise SnapshotCorruptionError("snapshot completion metadata is missing")
        rows = self._bounded_rows(self._connection.execute(
            "SELECT value FROM snapshot_meta WHERE key = ?", (key,)
        ), 1)
        if len(rows) != 1 or not isinstance(rows[0][0], str):
            raise SnapshotCorruptionError("snapshot completion metadata is missing")
        try:
            return json.loads(rows[0][0])
        except json.JSONDecodeError as error:
            raise SnapshotCorruptionError("snapshot metadata is malformed") from error

    def _validate_completed_snapshot(self) -> None:
        tables = self._bounded_rows(self._connection.execute("""
            SELECT name FROM sqlite_schema
            WHERE type = 'table' AND name IN ('directories', 'files', 'snapshot_meta')
            ORDER BY name
        """), 3)
        if [row[0] for row in tables] != ["directories", "files", "snapshot_meta"]:
            raise SnapshotCorruptionError("snapshot schema is invalid")
        columns = self._bounded_rows(
            self._connection.execute("PRAGMA table_info(directories)"), 16
        )
        actual = tuple((row[1], str(row[2]).upper()) for row in columns)
        if actual != _EXPECTED_COLUMNS or columns[0][5] != 1:
            raise SnapshotCorruptionError("snapshot directory schema is invalid")
        file_columns = self._bounded_rows(
            self._connection.execute("PRAGMA table_info(files)"), 8
        )
        if (
            tuple((row[1], str(row[2]).upper()) for row in file_columns)
            != _EXPECTED_FILE_COLUMNS or file_columns[0][5] != 1
        ):
            raise SnapshotCorruptionError("snapshot file schema is invalid")
        metadata_columns = self._bounded_rows(
            self._connection.execute("PRAGMA table_info(snapshot_meta)"), 4
        )
        if tuple((row[1], str(row[2]).upper()) for row in metadata_columns) != (
            ("key", "TEXT"), ("value", "TEXT")
        ) or metadata_columns[0][5] != 1:
            raise SnapshotCorruptionError("snapshot metadata schema is invalid")
        expected_indexes = {
            "directories_parent_order": (
                ("parent_path", 0), ("allocated_bytes", 1),
                ("name_fold", 0), ("path", 0),
            ),
            "directories_name_fold": (("name_fold", 0),),
            "directories_global_order": (
                ("allocated_bytes", 1), ("name_fold", 0), ("path", 0),
            ),
            "files_parent_order": (
                ("parent_path", 0), ("allocated_bytes", 1),
                ("name_fold", 0), ("path", 0),
            ),
        }
        for index_name, expected in expected_indexes.items():
            index_rows = self._bounded_rows(
                self._connection.execute(f"PRAGMA index_xinfo({index_name})"), 8
            )
            actual_index = tuple(
                (row[2], row[3]) for row in index_rows if row[5] == 1
            )
            if actual_index != expected:
                raise SnapshotCorruptionError("snapshot query index is invalid")
        if self._metadata_value("complete") is not True:
            raise SnapshotCorruptionError("snapshot is incomplete")
        if self._metadata_value("schema_version") != SQLITE_SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotCorruptionError("snapshot schema version is unsupported")
        snapshot_id = self._metadata_value("snapshot_id")
        if (
            not isinstance(snapshot_id, str) or len(snapshot_id) != 32
            or any(character not in "0123456789abcdef" for character in snapshot_id)
        ):
            raise SnapshotCorruptionError("snapshot identity is invalid")
        root_path = self._metadata_value("root_path")
        count = self._metadata_value("directory_count")
        if not isinstance(root_path, str) or not root_path:
            raise SnapshotCorruptionError("snapshot root metadata is invalid")
        self._validate_input_path(root_path)
        if (
            isinstance(count, bool) or not isinstance(count, int) or count <= 0
            or count > self.resource_limits.max_directories
        ):
            raise SnapshotCorruptionError("snapshot directory count is invalid")
        actual_count = self._connection.execute(
            "SELECT COUNT(*) FROM directories"
        ).fetchone()[0]
        root_count = self._connection.execute(
            "SELECT COUNT(*) FROM directories WHERE path = ?", (root_path,)
        ).fetchone()[0]
        if actual_count != count or root_count != 1:
            raise SnapshotCorruptionError("snapshot completion metadata is inconsistent")
        self.snapshot_id = snapshot_id
        self.root_path = root_path
        self.directory_count = count
        file_count = self._metadata_value("file_count")
        if (
            isinstance(file_count, bool) or not isinstance(file_count, int)
            or file_count < 0
        ):
            raise SnapshotCorruptionError("snapshot file count is invalid")
        actual_files = self._connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if actual_files != file_count:
            raise SnapshotCorruptionError("snapshot file metadata is inconsistent")
        self.file_count = file_count

    def _ensure_open_and_unchanged(self) -> None:
        if self._closed:
            raise SQLiteQueryError("snapshot reader is closed")
        try:
            details = os.stat(self.path, follow_symlinks=False)
        except OSError as error:
            raise SQLiteQueryError("snapshot database changed") from error
        current_version = int(
            self._connection.execute("PRAGMA data_version").fetchone()[0]
        )
        if (
            (details.st_dev, details.st_ino) != self._identity
            or details.st_mtime_ns != self._mtime_ns
            or details.st_size != self._size
            or current_version != self._data_version
        ):
            raise SQLiteQueryError("snapshot database changed")

    def _validate_input_path(self, path: str) -> None:
        if not isinstance(path, str) or not path:
            raise SQLiteQueryError("path must be a nonempty string")
        try:
            check_path(path, self.resource_limits)
        except ResourceLimitExceeded as error:
            raise SQLiteQueryError("path exceeds query string limit") from error
        if _encoded_size(path) > self.query_limits.max_string_bytes:
            raise SQLiteQueryError("path exceeds query string limit")

    def _row(self, raw: tuple[Any, ...]) -> DirectoryRow:
        if len(raw) != 7:
            raise SnapshotCorruptionError("directory row shape is invalid")
        path, parent, name, allocated, direct, children, warnings = raw
        if (
            not isinstance(path, str) or not path
            or (parent is not None and not isinstance(parent, str))
            or not isinstance(name, str) or not name
        ):
            raise SnapshotCorruptionError("directory strings are malformed")
        for value in (path, name):
            if _encoded_size(value) > self.query_limits.max_string_bytes:
                raise SnapshotCorruptionError("directory string exceeds limit")
        if parent is not None and _encoded_size(parent) > self.query_limits.max_string_bytes:
            raise SnapshotCorruptionError("directory string exceeds limit")
        numbers = (allocated, direct, children, warnings)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in numbers
        ):
            raise SnapshotCorruptionError("directory accounting is malformed")
        if path == self.root_path:
            if parent is not None:
                raise SnapshotCorruptionError("snapshot root has an unexpected parent")
        elif parent is None:
            raise SnapshotCorruptionError("directory has an unexpected null parent")
        return DirectoryRow(path, parent, name, allocated, direct, children, warnings)

    def _file_row(self, raw: tuple[Any, ...]) -> DirectoryRow:
        if len(raw) != 5:
            raise SnapshotCorruptionError("file row shape is invalid")
        path, parent, name, allocated, name_fold = raw
        row = DirectoryRow(path, parent, name, allocated, 0, 0, 0, "file")
        if any(not isinstance(value, str) or not value for value in (path, parent, name)):
            raise SnapshotCorruptionError("file strings are malformed")
        if any(_encoded_size(value) > self.query_limits.max_string_bytes for value in (path, parent, name)):
            raise SnapshotCorruptionError("file string exceeds limit")
        if isinstance(allocated, bool) or not isinstance(allocated, int) or allocated < 0:
            raise SnapshotCorruptionError("file accounting is malformed")
        if not isinstance(name_fold, str) or name_fold != name.casefold():
            raise SnapshotCorruptionError("file folded name is malformed")
        return row

    def metadata(self, path: str) -> DirectoryRow | None:
        self._ensure_open_and_unchanged()
        self._validate_input_path(path)
        rows = self._bounded_rows(self._connection.execute(
            f"SELECT {_ROW_SELECT} FROM directories WHERE path = ?", (path,)
        ), 1)
        return None if not rows else self._row(rows[0])

    def _parse_keyset(
        self, token: str | None, kind: str, scope: str,
    ) -> tuple[int, str, str] | None:
        if token is None:
            return None
        payload = _token_decode(token)
        if (
            payload.get("v") != 1 or payload.get("kind") != kind
            or payload.get("snapshot") != self.snapshot_id
            or payload.get("scope") != scope
        ):
            raise InvalidContinuationToken("stale or mismatched continuation token")
        key = payload.get("key")
        if (
            not isinstance(key, list) or len(key) != 3
            or isinstance(key[0], bool) or not isinstance(key[0], int)
            or key[0] < 0 or key[0] > 2**63 - 1
            or not isinstance(key[1], str)
            or not isinstance(key[2], str)
        ):
            raise InvalidContinuationToken("malformed continuation state")
        for value in key[1:]:
            if _encoded_size(value) > self.query_limits.max_string_bytes * 2:
                raise InvalidContinuationToken("continuation state exceeds limit")
        return key[0], key[1], key[2]

    def _keyset_token(
        self, kind: str, scope: str, key: tuple[int, str, str],
    ) -> str:
        return _token_encode({
            "v": 1, "kind": kind, "snapshot": self.snapshot_id,
            "scope": scope, "key": list(key),
        })

    def _validate_keyset_row(
        self,
        key: tuple[int, str, str] | None,
        *,
        parent_path: str | None = None,
        query_fold: str | None = None,
    ) -> None:
        if key is None:
            return
        clauses = [
            "path = ?", "allocated_bytes = ?", "name_fold = ?",
        ]
        parameters: list[Any] = [key[2], key[0], key[1]]
        if parent_path is not None:
            clauses.append("parent_path = ?")
            parameters.append(parent_path)
        if query_fold is not None:
            clauses.append("instr(name_fold, ?) > 0")
            parameters.append(query_fold)
        row = self._fetchone(self._connection.execute(
            "SELECT 1 FROM directories WHERE " + " AND ".join(clauses),
            parameters,
        ))
        if row is None:
            raise InvalidContinuationToken("continuation state is not in snapshot")

    def _pack(
        self,
        kind: str,
        candidates: Iterable[tuple[DirectoryRow, tuple[int, str, str]]],
        token_for: Callable[[tuple[int, str, str]], str],
        more_after_candidates: bool,
    ) -> QueryPage:
        rows: list[DirectoryRow] = []
        keys: list[tuple[int, str, str]] = []
        candidate_list = list(candidates)
        for row, key in candidate_list[:self.query_limits.max_rows]:
            tentative_rows = tuple(rows + [row])
            tentative = QueryPage(kind, tentative_rows, True, token_for(key))
            if len(encode_query_page(tentative, self.query_limits).encode("utf-8")) > self.query_limits.row_payload_budget:
                break
            rows.append(row)
            keys.append(key)
        if not rows and candidate_list:
            raise SQLiteQueryError("one legal row exceeds response packing budget")
        has_more = len(rows) < len(candidate_list) or more_after_candidates
        continuation = token_for(keys[-1]) if has_more and keys else None
        page = QueryPage(kind, tuple(rows), has_more, continuation)
        encoded_size = len(encode_query_page(page, self.query_limits).encode("utf-8"))
        if encoded_size > self.query_limits.row_payload_budget:
            raise SQLiteQueryError("query page exceeds packing budget")
        return page

    def children(
        self, parent_path: str, continuation: str | None = None,
    ) -> QueryPage:
        self._ensure_open_and_unchanged()
        self._validate_input_path(parent_path)
        key = None
        if continuation is not None:
            payload = _token_decode(continuation)
            if (
                payload.get("kind") != "children"
                or payload.get("snapshot") != self.snapshot_id
                or payload.get("scope") != parent_path
            ):
                raise InvalidContinuationToken("stale or mismatched continuation token")
            raw_key = payload.get("key")
            if payload.get("v") == 1 and isinstance(raw_key, list) and len(raw_key) == 3:
                raw_key = [0] + raw_key
            if (
                not isinstance(raw_key, list) or len(raw_key) != 4
                or raw_key[0] not in (0, 1) or isinstance(raw_key[1], bool)
                or not isinstance(raw_key[1], int) or raw_key[1] < 0
                or not isinstance(raw_key[2], str)
                or not isinstance(raw_key[3], str)
            ):
                raise InvalidContinuationToken("continuation state is not in snapshot")
            key = (raw_key[0], raw_key[1], raw_key[2], raw_key[3])
            table = "directories" if key[0] == 0 else "files"
            if self._fetchone(self._connection.execute(
                f"SELECT 1 FROM {table} WHERE path=? AND parent_path=? AND allocated_bytes=? AND name_fold=?",
                (key[3], parent_path, key[1], key[2]),
            )) is None:
                raise InvalidContinuationToken("continuation state is not in snapshot")
        parent = self.metadata(parent_path)
        if parent is None:
            return QueryPage("children", (), False, None)
        corrupt = self._fetchone(self._connection.execute("""
            SELECT 1 FROM directories WHERE parent_path=? AND
              (typeof(allocated_bytes) <> 'integer' OR allocated_bytes < 0)
            UNION ALL
            SELECT 1 FROM files WHERE parent_path=? AND
              (typeof(allocated_bytes) <> 'integer' OR allocated_bytes < 0)
            LIMIT 1
        """, (parent_path, parent_path)))
        if corrupt is not None:
            raise SnapshotCorruptionError("child accounting is malformed")
        token_group, token_bytes, token_fold, token_path = key or (0, 2**63 - 1, "", "")
        cursor = self._connection.execute(f"""
            SELECT * FROM (
              SELECT 0 AS item_kind, {_ROW_SELECT}, name_fold FROM directories
               WHERE parent_path = ? AND ? = 0 AND (allocated_bytes < ? OR
                    (allocated_bytes = ? AND (name_fold, path) > (?, ?)))
              UNION ALL
              SELECT 1 AS item_kind, path,parent_path,name,allocated_bytes,0,0,0,name_fold
                FROM files WHERE parent_path = ? AND (? < 1 OR
                    (? = 1 AND (allocated_bytes < ? OR
                    (allocated_bytes = ? AND (name_fold, path) > (?, ?)))))
            ) ORDER BY item_kind, allocated_bytes DESC, name_fold, path LIMIT ?
        """, (parent_path, token_group, token_bytes, token_bytes, token_fold, token_path,
              parent_path, token_group, token_group, token_bytes, token_bytes,
              token_fold, token_path, self.query_limits.max_rows + 1))
        raw_rows = self._fetchmany(cursor, self.query_limits.max_rows + 1)
        candidates = []
        for raw in raw_rows[:self.query_limits.max_rows]:
            group = raw[0]
            row = (
                self._row(raw[1:8]) if group == 0
                else self._file_row((raw[1], raw[2], raw[3], raw[4], raw[8]))
            )
            name_fold = raw[8]
            if not isinstance(name_fold, str) or name_fold != row.name.casefold():
                raise SnapshotCorruptionError("directory search name is malformed")
            candidates.append((row, (group, row.allocated_bytes, name_fold, row.path)))
        def child_token(value: tuple[int, int, str, str]) -> str:
            return _token_encode({"v": 2, "kind": "children", "snapshot": self.snapshot_id,
                                  "scope": parent_path, "key": list(value)})
        return self._pack(
            "children", candidates, child_token,
            len(raw_rows) > self.query_limits.max_rows,
        )

    def children_at(self, path: str) -> QueryPage:
        """Return a bounded sibling page beginning at an exact directory path."""
        self._ensure_open_and_unchanged()
        self._validate_input_path(path)
        target = self.metadata(path)
        if target is None:
            return QueryPage("childrenAt", (), False, None)
        if target.parent_path is None:
            return QueryPage("childrenAt", (target,), False, None)
        fold_row = self._fetchone(self._connection.execute(
            "SELECT name_fold FROM directories WHERE path = ?", (path,)
        ))
        if fold_row is None or not isinstance(fold_row[0], str):
            raise SnapshotCorruptionError("directory search name is malformed")
        name_fold = fold_row[0]
        if name_fold != target.name.casefold():
            raise SnapshotCorruptionError("directory search name is malformed")
        cursor = self._connection.execute(f"""
            SELECT * FROM (
                SELECT {_ROW_SELECT}, name_fold
                FROM directories
                WHERE parent_path = ? AND allocated_bytes < ?
                UNION ALL
                SELECT {_ROW_SELECT}, name_fold
                FROM directories
                WHERE parent_path = ? AND allocated_bytes = ?
                  AND (name_fold, path) >= (?, ?)
            )
            ORDER BY allocated_bytes DESC, name_fold, path
            LIMIT ?
        """, (
            target.parent_path, target.allocated_bytes,
            target.parent_path, target.allocated_bytes, name_fold, target.path,
            self.query_limits.max_rows + 1,
        ))
        raw_rows = self._fetchmany(cursor, self.query_limits.max_rows + 1)
        candidates = []
        for raw in raw_rows[:self.query_limits.max_rows]:
            row = self._row(raw[:7])
            folded = raw[7]
            if not isinstance(folded, str) or folded != row.name.casefold():
                raise SnapshotCorruptionError("directory search name is malformed")
            candidates.append((row, (row.allocated_bytes, folded, row.path)))
        if not candidates or candidates[0][0].path != path:
            raise SnapshotCorruptionError("direct child reveal did not locate target")
        return self._pack(
            "childrenAt", candidates,
            lambda value: self._keyset_token("children", target.parent_path, value),
            len(raw_rows) > self.query_limits.max_rows,
        )

    def _search_scope(self, query_fold: str) -> str:
        return base64.urlsafe_b64encode(query_fold.encode("utf-8")).decode()

    def search(
        self, query: str, continuation: str | None = None,
    ) -> QueryPage:
        self._ensure_open_and_unchanged()
        if not isinstance(query, str):
            raise SQLiteQueryError("search query must be a string")
        if _encoded_size(query) > self.query_limits.max_search_query_bytes:
            raise SQLiteQueryError("search query exceeds byte limit")
        query_fold = query.casefold()
        if not query_fold:
            return QueryPage("search", (), False, None)
        scope = self._search_scope(query_fold)
        key = self._parse_keyset(continuation, "search", scope)
        self._validate_keyset_row(key, query_fold=query_fold)
        if key is None:
            cursor = self._connection.execute(f"""
                SELECT {_ROW_SELECT}, name_fold
                FROM directories
                WHERE instr(name_fold, ?) > 0
                ORDER BY allocated_bytes DESC, name_fold, path
                LIMIT ?
            """, (query_fold, self.query_limits.max_rows + 1))
        else:
            cursor = self._connection.execute(f"""
                SELECT * FROM (
                    SELECT {_ROW_SELECT}, name_fold
                    FROM directories
                    WHERE allocated_bytes < ? AND instr(name_fold, ?) > 0
                    UNION ALL
                    SELECT {_ROW_SELECT}, name_fold
                    FROM directories
                    WHERE allocated_bytes = ? AND (name_fold, path) > (?, ?)
                      AND instr(name_fold, ?) > 0
                )
                ORDER BY allocated_bytes DESC, name_fold, path
                LIMIT ?
            """, (
                key[0], query_fold, key[0], key[1], key[2], query_fold,
                self.query_limits.max_rows + 1,
            ))
        raw_rows = self._fetchmany(cursor, self.query_limits.max_rows + 1)
        candidates = []
        for raw in raw_rows[:self.query_limits.max_rows]:
            row = self._row(raw[:7])
            name_fold = raw[7]
            if not isinstance(name_fold, str) or name_fold != row.name.casefold():
                raise SnapshotCorruptionError("directory search name is malformed")
            candidates.append((row, (row.allocated_bytes, name_fold, row.path)))
        return self._pack(
            "search", candidates,
            lambda value: self._keyset_token("search", scope, value),
            len(raw_rows) > self.query_limits.max_rows,
        )

    def _validate_ancestor_chain(self, path: str) -> None:
        row = self._fetchone(self._connection.execute("""
            WITH RECURSIVE chain(path, parent_path) AS (
                SELECT path, parent_path FROM directories WHERE path = ?
                UNION
                SELECT parent.path, parent.parent_path
                FROM directories AS parent
                JOIN chain AS child ON parent.path = child.parent_path
            )
            SELECT COUNT(*),
                   COALESCE(MAX(path = ?), 0)
            FROM chain
        """, (path, self.root_path)))
        if row is None:
            raise SnapshotCorruptionError("ancestor hierarchy is malformed")
        if int(row[0]) == 0:
            raise SnapshotCorruptionError("ancestor start path is missing")
        if int(row[1]) != 1:
            raise SnapshotCorruptionError(
                "ancestor hierarchy is cyclic, disconnected, or missing a parent"
            )

    def _parse_ancestor_token(
        self, token: str | None, original_path: str,
    ) -> str:
        if token is None:
            return original_path
        payload = _token_decode(token)
        if (
            payload.get("v") != 1 or payload.get("kind") != "ancestors"
            or payload.get("snapshot") != self.snapshot_id
            or payload.get("scope") != original_path
            or not isinstance(payload.get("next"), str)
        ):
            raise InvalidContinuationToken("stale or malformed ancestor token")
        next_path = payload["next"]
        self._validate_input_path(next_path)
        belongs = self._fetchone(self._connection.execute("""
            WITH RECURSIVE parents(path) AS (
                SELECT parent_path FROM directories
                WHERE path = ? AND parent_path IS NOT NULL
                UNION
                SELECT directory.parent_path
                FROM directories AS directory
                JOIN parents ON directory.path = parents.path
                WHERE directory.parent_path IS NOT NULL
            )
            SELECT 1 FROM parents WHERE path = ? LIMIT 1
        """, (original_path, next_path)))
        if belongs is None:
            raise InvalidContinuationToken("ancestor continuation is not in chain")
        return next_path

    def ancestors(
        self, path: str, continuation: str | None = None,
    ) -> QueryPage:
        self._ensure_open_and_unchanged()
        self._validate_input_path(path)
        start = self._parse_ancestor_token(continuation, path)
        self._validate_ancestor_chain(start)
        page_limit = min(
            self.query_limits.max_rows, self.query_limits.max_breadcrumb_rows
        )
        cursor = self._connection.execute(f"""
            WITH RECURSIVE chain(
                path, parent_path, name, allocated_bytes,
                direct_files_bytes, child_count, warning_count, ordinal
            ) AS (
                SELECT {_ROW_SELECT}, 0 FROM directories WHERE path = ?
                UNION ALL
                SELECT parent.path, parent.parent_path, parent.name,
                       parent.allocated_bytes, parent.direct_files_bytes,
                       parent.child_count, parent.warning_count, child.ordinal + 1
                FROM directories AS parent
                JOIN chain AS child ON parent.path = child.parent_path
            )
            SELECT path, parent_path, name, allocated_bytes,
                   direct_files_bytes, child_count, warning_count
            FROM chain
            ORDER BY ordinal
            LIMIT ?
        """, (start, page_limit + 1))
        raw_rows = self._fetchmany(cursor, page_limit + 1)
        rows = tuple(self._row(raw) for raw in raw_rows[:page_limit])
        has_more = len(raw_rows) > page_limit
        continuation_value = None
        if has_more:
            next_path = raw_rows[page_limit][0]
            continuation_value = _token_encode({
                "v": 1, "kind": "ancestors", "snapshot": self.snapshot_id,
                "scope": path, "next": next_path,
            })
        page = QueryPage("ancestors", rows, has_more, continuation_value)
        if len(encode_query_page(page, self.query_limits).encode("utf-8")) <= self.query_limits.row_payload_budget:
            return page

        packed: list[DirectoryRow] = []
        for row in rows:
            next_path = row.parent_path
            tentative_more = next_path is not None
            token = self._ancestor_token(path, next_path) if tentative_more else None
            tentative = QueryPage(
                "ancestors", tuple(packed + [row]), tentative_more, token
            )
            if len(encode_query_page(tentative, self.query_limits).encode("utf-8")) > self.query_limits.row_payload_budget:
                break
            packed.append(row)
        if not packed and rows:
            raise SQLiteQueryError("one legal row exceeds response packing budget")
        packed_more = len(packed) < len(rows) or has_more
        packed_next = packed[-1].parent_path if packed_more and packed else None
        result = QueryPage(
            "ancestors", tuple(packed), packed_more,
            self._ancestor_token(path, packed_next) if packed_next else None,
        )
        if len(encode_query_page(result, self.query_limits).encode("utf-8")) > self.query_limits.row_payload_budget:
            raise SQLiteQueryError("query page exceeds packing budget")
        return result

    def _ancestor_token(self, original_path: str, next_path: str) -> str:
        return _token_encode({
            "v": 1, "kind": "ancestors", "snapshot": self.snapshot_id,
            "scope": original_path, "next": next_path,
        })

    def interrupt(self) -> None:
        self._connection.interrupt()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "SQLiteSnapshotReader":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
