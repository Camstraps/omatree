"""Path validation and mount-boundary policy for OmaTree scans."""

from __future__ import annotations

import os
import stat
from typing import Any

from .discovery import flatten_findmnt


def canonical_path(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def path_is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((canonical_path(path), canonical_path(parent))) == canonical_path(parent)
    except ValueError:
        return False


def validate_scan_root(path: str, mountpoint: str) -> str | None:
    """Return the existing CLI error text when a scan root is invalid."""
    if not path_is_within(path, mountpoint):
        return "scan path is outside the selected filesystem mountpoint"
    try:
        path_stats = os.stat(path, follow_symlinks=False)
    except OSError:
        path_stats = None
    if path_stats is None or not stat.S_ISDIR(path_stats.st_mode):
        return "scan path is not an accessible directory"
    if os.path.realpath(path) != path:
        return "scan path may not contain symbolic-link components"
    return None


def descendant_mountpoints(
    mountpoint: str, findmnt_data: dict[str, Any],
) -> set[str]:
    root = canonical_path(mountpoint)
    descendants: set[str] = set()
    for mount in flatten_findmnt(findmnt_data.get("filesystems") or []):
        target = str(mount.get("target") or "")
        if not target:
            continue
        normalized = canonical_path(target)
        if normalized != root and path_is_within(normalized, root):
            descendants.add(normalized)
    return descendants


def is_excluded(path: str, excluded_mounts: set[str]) -> bool:
    normalized = canonical_path(path)
    return any(
        normalized == mount or path_is_within(normalized, mount)
        for mount in excluded_mounts
    )
