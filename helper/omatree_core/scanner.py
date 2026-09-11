"""Iterative, filesystem-aware directory scanning for OmaTree."""

from __future__ import annotations

import os
import stat
import time
from typing import Any, Callable

from . import protocol
from .paths import canonical_path, is_excluded
from .resources import (
    DEFAULT_LIMITS,
    ResourceLimitExceeded,
    ResourceLimits,
    check_path,
)


MAX_WARNINGS = 100


class ScanCancelled(Exception):
    """Raised internally when a scan has been cancelled."""


class Cancellation:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self, _signum: int | None = None, _frame: Any = None) -> None:
        self.cancelled = True

    def check(self) -> None:
        if self.cancelled:
            raise ScanCancelled


class ScanReporter:
    def __init__(
        self,
        request_id: str,
        emit: Callable[[dict[str, Any]], None],
        clock: Callable[[], float] = time.monotonic,
        progress_interval: float = 0.25,
        limits: ResourceLimits = DEFAULT_LIMITS,
    ) -> None:
        self.request_id = request_id
        self.emit = emit
        self.clock = clock
        self.progress_interval = progress_interval
        self.limits = limits
        self.entries = 0
        self.bytes = 0
        self.warning_count = 0
        self._last_progress = clock()

    def account(self, allocated_bytes: int) -> None:
        if self.entries >= self.limits.max_entries:
            raise ResourceLimitExceeded("scanned entries", self.limits.max_entries)
        self.entries += 1
        self.bytes += allocated_bytes
        now = self.clock()
        if now - self._last_progress >= self.progress_interval:
            self.emit(protocol.progress_event(
                self.request_id, self.entries, self.bytes
            ))
            self._last_progress = now

    def warning(self, path: str, error: OSError | str) -> None:
        self.warning_count += 1
        if self.warning_count <= MAX_WARNINGS:
            self.emit(protocol.warning_event(self.request_id, path, error))


def allocated_size(stats: os.stat_result) -> int:
    blocks = getattr(stats, "st_blocks", None)
    return int(blocks * 512) if blocks is not None else int(stats.st_size)


def scan_directory(
    path: str,
    excluded_mounts: set[str],
    reporter: ScanReporter,
    cancellation: Cancellation,
    limits: ResourceLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Measure a complete directory tree and return its directory records."""
    directories: list[dict[str, Any]] = []
    summary = scan_tree(
        path, excluded_mounts, reporter, cancellation, directories.append, limits
    )
    summary["directories"] = directories
    root = canonical_path(path)
    summary["children"] = sorted(
        (
            {
                "name": record["name"],
                "path": record["path"],
                "bytes": record["bytes"],
                "kind": "directory",
            }
            for record in directories
            if record["parentPath"] == root
        ),
        key=lambda child: (-child["bytes"], child["name"].casefold()),
    )
    return summary


def scan_tree(
    path: str,
    excluded_mounts: set[str],
    reporter: ScanReporter,
    cancellation: Cancellation,
    emit_directory: Callable[[dict[str, Any]], None],
    limits: ResourceLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Scan once and emit finalized directory aggregates in post-order."""
    root = canonical_path(path)
    exclusions = {canonical_path(item) for item in excluded_mounts}
    seen_hardlinks: set[tuple[int, int]] = set()
    directory_count = 0
    discovered_directory_count = 1
    reporter.limits = limits
    check_path(root, limits)
    if discovered_directory_count > limits.max_directories:
        raise ResourceLimitExceeded("directories", limits.max_directories)

    def count_stat(stats: os.stat_result) -> int:
        if stat.S_ISREG(stats.st_mode) and stats.st_nlink > 1:
            key = (stats.st_dev, stats.st_ino)
            if key in seen_hardlinks:
                reporter.account(0)
                return 0
            if len(seen_hardlinks) >= limits.max_hardlink_identities:
                raise ResourceLimitExceeded(
                    "hardlink identities", limits.max_hardlink_identities
                )
            seen_hardlinks.add(key)
        value = allocated_size(stats)
        reporter.account(value)
        return value

    def open_frame(
        directory_path: str,
        name: str,
        parent_path: str | None,
        directory_bytes: int,
    ) -> dict[str, Any]:
        warning_start = reporter.warning_count
        try:
            iterator = os.scandir(directory_path)
        except OSError as error:
            reporter.warning(directory_path, error)
            iterator = None
        return {
            "path": directory_path,
            "name": name,
            "parentPath": parent_path,
            "iterator": iterator,
            "bytes": directory_bytes,
            "directFilesBytes": 0,
            "childDirectoryCount": 0,
            "warningStart": warning_start,
        }

    try:
        root_stats = os.stat(root, follow_symlinks=False)
    except OSError as error:
        reporter.warning(root, error)
        return {"bytes": 0, "directFilesBytes": 0, "directoryCount": 0}

    stack = [open_frame(
        root,
        os.path.basename(root.rstrip(os.sep)) or root,
        None,
        count_stat(root_stats),
    )]
    try:
        while stack:
            cancellation.check()
            frame = stack[-1]
            iterator = frame["iterator"]
            if iterator is None:
                entry = None
            else:
                try:
                    entry = next(iterator)
                except StopIteration:
                    iterator.close()
                    frame["iterator"] = None
                    entry = None
                except OSError as error:
                    reporter.warning(frame["path"], error)
                    iterator.close()
                    frame["iterator"] = None
                    entry = None

            if entry is None:
                record = {
                    "path": frame["path"],
                    "parentPath": frame["parentPath"],
                    "name": frame["name"],
                    "bytes": frame["bytes"],
                    "directFilesBytes": frame["directFilesBytes"],
                    "childDirectoryCount": frame["childDirectoryCount"],
                    "warningCount": reporter.warning_count - frame["warningStart"],
                }
                emit_directory(record)
                directory_count += 1
                stack.pop()
                if stack:
                    stack[-1]["bytes"] += record["bytes"]
                    stack[-1]["childDirectoryCount"] += 1
                else:
                    return {
                        "bytes": record["bytes"],
                        "directFilesBytes": record["directFilesBytes"],
                        "directoryCount": directory_count,
                    }
                continue

            entry_path = entry.path
            check_path(entry_path, limits)
            if is_excluded(entry_path, exclusions):
                continue
            try:
                stats = entry.stat(follow_symlinks=False)
            except OSError as error:
                reporter.warning(entry_path, error)
                continue

            if stat.S_ISDIR(stats.st_mode):
                if discovered_directory_count >= limits.max_directories:
                    raise ResourceLimitExceeded(
                        "directories", limits.max_directories
                    )
                discovered_directory_count += 1
                stack.append(open_frame(
                    entry_path,
                    entry.name,
                    frame["path"],
                    count_stat(stats),
                ))
            else:
                value = count_stat(stats)
                frame["bytes"] += value
                frame["directFilesBytes"] += value
    finally:
        for frame in stack:
            iterator = frame["iterator"]
            if iterator is not None:
                iterator.close()

    raise RuntimeError("directory scan ended without a root result")
