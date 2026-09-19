"""Private per-user persistence for completed broker snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
from typing import Mapping

from .sqlite_query import SQLiteSnapshotReader
from .sqlite_snapshot import SQLiteSnapshot, SQLiteSnapshotError


def ensure_snapshot_cache_dir(
    cache_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    if cache_dir is None:
        base = environment.get("XDG_CACHE_HOME")
        if not base:
            home = environment.get("HOME")
            if not home:
                raise SQLiteSnapshotError("HOME is required for persistent snapshots")
            base = os.path.join(home, ".cache")
        cache_dir = os.path.join(base, "omatree")
    target = Path(os.path.abspath(os.fspath(cache_dir)))
    parent = target.parent
    if Path(os.path.realpath(parent)) != parent:
        raise SQLiteSnapshotError("snapshot cache parent may not use symlinks")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.mkdir(mode=0o700, exist_ok=True)
    details = os.lstat(target)
    if stat.S_ISDIR(details.st_mode) and details.st_uid == os.getuid():
        os.chmod(target, 0o700)
        details = os.lstat(target)
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or Path(os.path.realpath(target)) != target):
        raise SQLiteSnapshotError("snapshot cache directory is unsafe")
    os.chmod(target, 0o700)
    return target


def root_key(root_path: str, mountpoint: str) -> str:
    return hashlib.sha256((mountpoint + "\0" + root_path).encode("utf-8")).hexdigest()


class PersistentSnapshotStore:
    def __init__(self, cache_dir: str | os.PathLike[str] | None = None) -> None:
        self.root = ensure_snapshot_cache_dir(cache_dir)

    def _pointer(self, key: str) -> Path:
        return self.root / ("active-" + key + ".json")

    def load(self, root_path: str, mountpoint: str) -> SQLiteSnapshot | None:
        key = root_key(root_path, mountpoint)
        pointer = self._pointer(key)
        try:
            info = json.loads(pointer.read_text(encoding="utf-8"))
            filename = info["database"]
            if not isinstance(filename, str) or "/" in filename or not filename.startswith("snapshot-"):
                raise ValueError
            path = self.root / filename
            with SQLiteSnapshotReader.open(path) as reader:
                if reader.root_path != root_path:
                    raise ValueError
                stored_mount = reader._metadata_value("mountpoint")
                if stored_mount != mountpoint:
                    raise ValueError
                for key in (
                    "entries", "allocated_bytes", "direct_files_bytes",
                    "warning_count", "duration_ms", "created_at_ms",
                ):
                    value = reader._metadata_value(key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                        raise ValueError
                details = os.lstat(path)
                return SQLiteSnapshot(path, reader.directory_count, reader.root_path,
                                      (details.st_dev, details.st_ino), reader.file_count)
        except (OSError, ValueError, KeyError, json.JSONDecodeError, SQLiteSnapshotError):
            return None
        except Exception:
            return None

    def publish(self, source: SQLiteSnapshot, mountpoint: str) -> SQLiteSnapshot:
        key = root_key(source.root_path, mountpoint)
        filename = f"snapshot-{key}-{secrets.token_hex(12)}.sqlite"
        final = self.root / filename
        temporary: Path | None = None
        moved_source = False
        try:
            if source.path.parent == self.root:
                os.replace(source.path, final)
                moved_source = True
            else:
                descriptor, temporary_name = tempfile.mkstemp(prefix=".snapshot-", dir=self.root)
                temporary = Path(temporary_name)
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as target, source.path.open("rb") as origin:
                    shutil.copyfileobj(origin, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, final)
                temporary = None
            with SQLiteSnapshotReader.open(final) as reader:
                if reader.root_path != source.root_path or reader._metadata_value("mountpoint") != mountpoint:
                    raise SQLiteSnapshotError("persisted snapshot identity is invalid")
                details = os.lstat(final)
                result = SQLiteSnapshot(final, reader.directory_count, reader.root_path,
                                        (details.st_dev, details.st_ino), reader.file_count)
            pointer_tmp = self.root / (".pointer-" + secrets.token_hex(12))
            fd = os.open(pointer_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"database": filename}, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pointer_tmp, self._pointer(key))
            return result
        except BaseException:
            if moved_source:
                try:
                    os.replace(final, source.path)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            raise
