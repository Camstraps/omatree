"""Bounded, generation-specific SQLite snapshots for OmaTree directories."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import stat
import secrets
import tempfile
from typing import Any, Mapping

from .resources import DEFAULT_LIMITS, ResourceLimitExceeded, ResourceLimits, check_path


@dataclass(frozen=True, slots=True)
class SQLiteSnapshotLimits:
    max_database_bytes: int = 2 * 1024 * 1024 * 1024
    page_size: int = 4096
    cache_kib: int = 16 * 1024
    busy_timeout_ms: int = 2000
    max_metadata_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if self.max_database_bytes < self.page_size:
            raise ValueError("max_database_bytes must hold at least one page")
        if self.page_size <= 0 or self.cache_kib <= 0 or self.busy_timeout_ms <= 0:
            raise ValueError("SQLite snapshot limits must be positive")
        if self.max_metadata_bytes <= 0:
            raise ValueError("max_metadata_bytes must be positive")


DEFAULT_SQLITE_LIMITS = SQLiteSnapshotLimits()
SQLITE_SNAPSHOT_SCHEMA_VERSION = 2


class SQLiteSnapshotError(RuntimeError):
    """Raised when a staging snapshot cannot be safely completed."""


class SQLiteSnapshotValidationError(SQLiteSnapshotError):
    """Raised when staged directory relationships are inconsistent."""


def _validate_private_directory(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    if Path(os.path.realpath(absolute)) != absolute:
        raise SQLiteSnapshotError("snapshot runtime directory may not use symlinks")
    try:
        details = os.lstat(absolute)
    except OSError as error:
        raise SQLiteSnapshotError("snapshot runtime directory is unavailable") from error
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise SQLiteSnapshotError("snapshot runtime directory has unsafe ownership")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise SQLiteSnapshotError("snapshot runtime directory is not private")


def ensure_snapshot_runtime_dir(
    runtime_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return a private, non-symlinked ``$XDG_RUNTIME_DIR/omatree`` directory."""
    environment = os.environ if environ is None else environ
    base_value = runtime_dir if runtime_dir is not None else environment.get("XDG_RUNTIME_DIR")
    if not base_value:
        raise SQLiteSnapshotError("XDG_RUNTIME_DIR is required for SQLite snapshots")
    base = Path(os.path.abspath(os.fspath(base_value)))
    _validate_private_directory(base)
    target = base / "omatree"
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise SQLiteSnapshotError("could not create snapshot runtime directory") from error
    _validate_private_directory(target)
    return target


def _create_snapshot_file(runtime_root: Path) -> tuple[Path, tuple[int, int]]:
    descriptor, raw_path = tempfile.mkstemp(
        prefix="generation-", suffix=".sqlite", dir=runtime_root
    )
    try:
        path = Path(raw_path)
        if path.parent != runtime_root or Path(os.path.realpath(path.parent)) != runtime_root:
            raise SQLiteSnapshotError("snapshot file escaped its runtime directory")
        os.fchmod(descriptor, 0o600)
        details = os.fstat(descriptor)
        path_details = os.lstat(path)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or not stat.S_ISREG(path_details.st_mode)
            or (path_details.st_dev, path_details.st_ino)
                != (details.st_dev, details.st_ino)
        ):
            raise SQLiteSnapshotError("snapshot file has unsafe ownership")
        return path, (details.st_dev, details.st_ino)
    finally:
        os.close(descriptor)


def _unlink_if_original(path: Path, identity: tuple[int, int]) -> None:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == os.getuid()
        and (details.st_dev, details.st_ino) == identity
    ):
        path.unlink()


@dataclass(slots=True)
class SQLiteSnapshot:
    """A completed snapshot file whose lifetime is explicitly controlled."""

    path: Path
    directory_count: int
    root_path: str
    _identity: tuple[int, int]
    file_count: int = 0

    def delete(self) -> None:
        _unlink_if_original(self.path, self._identity)


class SQLiteSnapshotWriter:
    """Insert finalized scanner records without retaining a Python tree."""

    def __init__(
        self,
        path: Path,
        identity: tuple[int, int],
        connection: sqlite3.Connection,
        resource_limits: ResourceLimits,
        sqlite_limits: SQLiteSnapshotLimits,
    ) -> None:
        self.path = path
        self._identity = identity
        self._connection = connection
        self._resource_limits = resource_limits
        self._sqlite_limits = sqlite_limits
        self._directory_count = 0
        self._file_count = 0
        self._closed = False
        self._completed = False
        self._snapshot_id = secrets.token_hex(16)

    @classmethod
    def create(
        cls,
        runtime_dir: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        resource_limits: ResourceLimits = DEFAULT_LIMITS,
        sqlite_limits: SQLiteSnapshotLimits = DEFAULT_SQLITE_LIMITS,
    ) -> "SQLiteSnapshotWriter":
        runtime_root = ensure_snapshot_runtime_dir(runtime_dir, environ)
        path, identity = _create_snapshot_file(runtime_root)
        try:
            connection = sqlite3.connect(
                path,
                timeout=sqlite_limits.busy_timeout_ms / 1000,
                isolation_level=None,
            )
            writer = cls(
                path, identity, connection, resource_limits, sqlite_limits
            )
            writer._configure()
            writer._create_schema()
            connection.execute("BEGIN IMMEDIATE")
            return writer
        except BaseException as error:
            try:
                connection.close()
            except (UnboundLocalError, sqlite3.Error):
                pass
            _unlink_if_original(path, identity)
            if isinstance(error, sqlite3.OperationalError) and "full" in str(error).lower():
                raise ResourceLimitExceeded(
                    "SQLite snapshot bytes", sqlite_limits.max_database_bytes
                ) from error
            raise

    @classmethod
    def create_in_directory(
        cls,
        snapshot_directory: str | os.PathLike[str],
        *,
        resource_limits: ResourceLimits = DEFAULT_LIMITS,
        sqlite_limits: SQLiteSnapshotLimits = DEFAULT_SQLITE_LIMITS,
    ) -> "SQLiteSnapshotWriter":
        """Create a staging database in an already-private broker session."""
        runtime_root = Path(os.path.abspath(os.fspath(snapshot_directory)))
        _validate_private_directory(runtime_root)
        path, identity = _create_snapshot_file(runtime_root)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                path,
                timeout=sqlite_limits.busy_timeout_ms / 1000,
                isolation_level=None,
            )
            writer = cls(
                path, identity, connection, resource_limits, sqlite_limits
            )
            writer._configure()
            writer._create_schema()
            connection.execute("BEGIN IMMEDIATE")
            return writer
        except BaseException as error:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            _unlink_if_original(path, identity)
            if isinstance(error, sqlite3.OperationalError) and "full" in str(error).lower():
                raise ResourceLimitExceeded(
                    "SQLite snapshot bytes", sqlite_limits.max_database_bytes
                ) from error
            raise

    def _configure(self) -> None:
        connection = self._connection
        connection.enable_load_extension(False)
        connection.execute(f"PRAGMA page_size = {self._sqlite_limits.page_size}")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(f"PRAGMA cache_size = -{self._sqlite_limits.cache_kib}")
        connection.execute("PRAGMA temp_store = FILE")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            f"PRAGMA busy_timeout = {self._sqlite_limits.busy_timeout_ms}"
        )
        connection.execute("PRAGMA trusted_schema = OFF")
        maximum_pages = (
            self._sqlite_limits.max_database_bytes // self._sqlite_limits.page_size
        )
        connection.execute(f"PRAGMA max_page_count = {maximum_pages}")

    def _create_schema(self) -> None:
        self._connection.executescript("""
            CREATE TABLE directories (
                path TEXT PRIMARY KEY,
                parent_path TEXT,
                name TEXT NOT NULL,
                name_fold TEXT NOT NULL,
                allocated_bytes INTEGER NOT NULL CHECK (allocated_bytes >= 0),
                direct_files_bytes INTEGER NOT NULL CHECK (direct_files_bytes >= 0),
                child_count INTEGER NOT NULL CHECK (child_count >= 0),
                file_count INTEGER NOT NULL CHECK (file_count >= 0),
                warning_count INTEGER NOT NULL CHECK (warning_count >= 0),
                depth INTEGER CHECK (depth IS NULL OR depth >= 0)
            ) WITHOUT ROWID;
            CREATE TABLE snapshot_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                parent_path TEXT NOT NULL,
                name TEXT NOT NULL,
                name_fold TEXT NOT NULL,
                allocated_bytes INTEGER NOT NULL CHECK (allocated_bytes >= 0)
            ) WITHOUT ROWID;
        """)

    def _ensure_open(self) -> None:
        if self._closed or self._completed:
            raise SQLiteSnapshotError("snapshot writer is not open")

    @staticmethod
    def _integer(record: Mapping[str, Any], key: str) -> int:
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SQLiteSnapshotError(f"invalid directory field: {key}")
        return value

    def _check_size(self) -> None:
        page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        if page_count * page_size > self._sqlite_limits.max_database_bytes:
            raise ResourceLimitExceeded(
                "SQLite snapshot bytes", self._sqlite_limits.max_database_bytes
            )

    def _fail(self, error: BaseException) -> None:
        try:
            self._connection.rollback()
        except sqlite3.Error:
            pass
        self.close(delete=True)
        raise error

    def insert_directory(self, record: Mapping[str, Any]) -> None:
        """Insert one finalized post-order directory record immediately."""
        self._ensure_open()
        try:
            if self._directory_count >= self._resource_limits.max_directories:
                raise ResourceLimitExceeded(
                    "directories", self._resource_limits.max_directories
                )
            path = record.get("path")
            parent = record.get("parentPath")
            name = record.get("name")
            if not isinstance(path, str) or not path:
                raise SQLiteSnapshotError("directory path must be a nonempty string")
            if parent is not None and not isinstance(parent, str):
                raise SQLiteSnapshotError("directory parent path must be a string or null")
            if not isinstance(name, str) or not name:
                raise SQLiteSnapshotError("directory name must be a nonempty string")
            check_path(path, self._resource_limits)
            if parent is not None:
                check_path(parent, self._resource_limits)
            check_path(name, self._resource_limits)
            values = (
                path,
                parent,
                name,
                name.casefold(),
                self._integer(record, "bytes"),
                self._integer(record, "directFilesBytes"),
                self._integer(record, "childDirectoryCount"),
                self._integer(record, "fileCount") if "fileCount" in record else 0,
                self._integer(record, "warningCount"),
            )
            self._connection.execute(
                """INSERT INTO directories (
                       path, parent_path, name, name_fold, allocated_bytes,
                       direct_files_bytes, child_count, file_count,
                       warning_count, depth
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                values,
            )
            self._directory_count += 1
            self._check_size()
        except sqlite3.IntegrityError as error:
            self._fail(SQLiteSnapshotError("duplicate or invalid directory record"))
        except sqlite3.OperationalError as error:
            if "full" in str(error).lower():
                self._fail(ResourceLimitExceeded(
                    "SQLite snapshot bytes",
                    self._sqlite_limits.max_database_bytes,
                ))
            self._fail(SQLiteSnapshotError(str(error)))
        except (SQLiteSnapshotError, ResourceLimitExceeded) as error:
            self._fail(error)

    def insert_file(self, record: Mapping[str, Any]) -> None:
        """Insert one immediate regular-file row without retaining it in Python."""
        self._ensure_open()
        try:
            path = record.get("path")
            parent = record.get("parentPath")
            name = record.get("name")
            if not all(isinstance(value, str) and value for value in (path, parent, name)):
                raise SQLiteSnapshotError("file path, parent, and name must be strings")
            check_path(path, self._resource_limits)
            check_path(parent, self._resource_limits)
            check_path(name, self._resource_limits)
            self._connection.execute(
                """INSERT INTO files(path,parent_path,name,name_fold,allocated_bytes)
                   VALUES (?,?,?,?,?)""",
                (path, parent, name, name.casefold(), self._integer(record, "bytes")),
            )
            self._file_count += 1
            self._check_size()
        except sqlite3.IntegrityError:
            self._fail(SQLiteSnapshotError("duplicate or invalid file record"))
        except sqlite3.OperationalError as error:
            if "full" in str(error).lower():
                self._fail(ResourceLimitExceeded(
                    "SQLite snapshot bytes", self._sqlite_limits.max_database_bytes
                ))
            self._fail(SQLiteSnapshotError(str(error)))
        except (SQLiteSnapshotError, ResourceLimitExceeded) as error:
            self._fail(error)

    def set_metadata(self, key: str, value: Any) -> None:
        self._ensure_open()
        if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 128:
            self._fail(SQLiteSnapshotError("invalid snapshot metadata key"))
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > self._sqlite_limits.max_metadata_bytes:
            self._fail(ResourceLimitExceeded(
                "SQLite snapshot metadata bytes",
                self._sqlite_limits.max_metadata_bytes,
            ))
        try:
            self._connection.execute(
                "INSERT OR REPLACE INTO snapshot_meta(key, value) VALUES (?, ?)",
                (key, encoded),
            )
            self._check_size()
        except sqlite3.Error as error:
            self._fail(SQLiteSnapshotError(str(error)))

    def _create_indexes(self) -> None:
        self._connection.execute("""
            CREATE INDEX directories_parent_order
            ON directories(parent_path, allocated_bytes DESC, name_fold, path)
        """)
        self._connection.execute("""
            CREATE INDEX files_parent_order
            ON files(parent_path, allocated_bytes DESC, name_fold, path)
        """)
        self._connection.execute(
            "CREATE INDEX directories_name_fold ON directories(name_fold)"
        )
        self._connection.execute("""
            CREATE INDEX directories_global_order
            ON directories(allocated_bytes DESC, name_fold, path)
        """)
        self._check_size()

    def _validate(self, root_path: str, expected_count: int) -> None:
        connection = self._connection
        actual_count = int(connection.execute(
            "SELECT COUNT(*) FROM directories"
        ).fetchone()[0])
        if actual_count != expected_count or actual_count != self._directory_count:
            raise SQLiteSnapshotValidationError("directory count does not match scan summary")
        root_count = int(connection.execute(
            "SELECT COUNT(*) FROM directories WHERE path = ?", (root_path,)
        ).fetchone()[0])
        if root_count != 1:
            raise SQLiteSnapshotValidationError("expected root directory is missing")
        unexpected_roots = int(connection.execute(
            "SELECT COUNT(*) FROM directories WHERE parent_path IS NULL AND path <> ?",
            (root_path,),
        ).fetchone()[0])
        root_parent = connection.execute(
            "SELECT parent_path FROM directories WHERE path = ?", (root_path,)
        ).fetchone()[0]
        if unexpected_roots or root_parent is not None:
            raise SQLiteSnapshotValidationError("unexpected null-parent directory")
        missing_parent = connection.execute("""
            SELECT child.path
            FROM directories AS child
            LEFT JOIN directories AS parent ON parent.path = child.parent_path
            WHERE child.parent_path IS NOT NULL AND parent.path IS NULL
            LIMIT 1
        """).fetchone()
        if missing_parent is not None:
            raise SQLiteSnapshotValidationError("directory has no parent")
        bad_children = connection.execute("""
            SELECT parent.path
            FROM directories AS parent
            LEFT JOIN (
                SELECT parent_path, COUNT(*) AS actual_count
                FROM directories
                WHERE parent_path IS NOT NULL
                GROUP BY parent_path
            ) AS children ON children.parent_path = parent.path
            WHERE parent.child_count <> COALESCE(children.actual_count, 0)
            LIMIT 1
        """).fetchone()
        if bad_children is not None:
            raise SQLiteSnapshotValidationError("directory child count is inconsistent")
        bad_files = connection.execute("""
            SELECT parent.path FROM directories AS parent
            LEFT JOIN (
                SELECT parent_path, COUNT(*) AS actual_count FROM files
                GROUP BY parent_path
            ) AS children ON children.parent_path = parent.path
            WHERE parent.file_count <> COALESCE(children.actual_count, 0)
            LIMIT 1
        """).fetchone()
        if bad_files is not None:
            raise SQLiteSnapshotValidationError("directory file count is inconsistent")
        reachable_count = int(connection.execute("""
            WITH RECURSIVE reachable(path) AS (
                SELECT path FROM directories WHERE path = ?
                UNION
                SELECT child.path
                FROM directories AS child
                JOIN reachable AS parent ON child.parent_path = parent.path
            )
            SELECT COUNT(*) FROM reachable
        """, (root_path,)).fetchone()[0])
        if reachable_count != actual_count:
            raise SQLiteSnapshotValidationError(
                "directory hierarchy is cyclic or disconnected"
            )
        missing_file_parent = connection.execute("""
            SELECT files.path FROM files LEFT JOIN directories
              ON directories.path = files.parent_path
            WHERE directories.path IS NULL LIMIT 1
        """).fetchone()
        if missing_file_parent is not None:
            raise SQLiteSnapshotValidationError("file has no parent directory")
        collision = connection.execute("""
            SELECT files.path FROM files JOIN directories USING(path) LIMIT 1
        """).fetchone()
        if collision is not None:
            raise SQLiteSnapshotValidationError("file and directory paths collide")

    def finalize(
        self,
        root_path: str,
        expected_count: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> SQLiteSnapshot:
        """Validate, commit, close, and return a completed snapshot handle."""
        self._ensure_open()
        try:
            check_path(root_path, self._resource_limits)
            self._create_indexes()
            self._validate(root_path, expected_count)
            for key, value in (metadata or {}).items():
                self.set_metadata(key, value)
            self.set_metadata("root_path", root_path)
            self.set_metadata("directory_count", expected_count)
            self.set_metadata("file_count", self._file_count)
            self.set_metadata("schema_version", SQLITE_SNAPSHOT_SCHEMA_VERSION)
            self.set_metadata("snapshot_id", self._snapshot_id)
            self.set_metadata("complete", True)
            self._check_size()
            self._connection.commit()
            self._completed = True
            self._connection.close()
            self._closed = True
            return SQLiteSnapshot(
                self.path, expected_count, root_path, self._identity, self._file_count
            )
        except sqlite3.OperationalError as error:
            if "full" in str(error).lower():
                self._fail(ResourceLimitExceeded(
                    "SQLite snapshot bytes",
                    self._sqlite_limits.max_database_bytes,
                ))
            self._fail(SQLiteSnapshotError(str(error)))
        except (SQLiteSnapshotError, ResourceLimitExceeded) as error:
            self._fail(error)

    def close(self, delete: bool = False) -> None:
        if not self._closed:
            if not self._completed:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
            self._connection.close()
            self._closed = True
        if delete:
            _unlink_if_original(self.path, self._identity)

    def abort(self) -> None:
        self.close(delete=True)

    def __enter__(self) -> "SQLiteSnapshotWriter":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if not self._completed:
            self.abort()
