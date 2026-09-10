#!/usr/bin/env python3
"""Discover user-relevant mounted filesystems for OmaTree."""

from __future__ import annotations

import json
import os
import argparse
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Callable


PSEUDO_FILESYSTEMS = {
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "fuse.gvfsd-fuse",
    "fuse.portal",
    "hugetlbfs",
    "mqueue",
    "proc",
    "pstore",
    "ramfs",
    "securityfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}

NETWORK_FILESYSTEMS = {
    "9p",
    "afs",
    "ceph",
    "cifs",
    "davfs",
    "davfs2",
    "fuse.sshfs",
    "glusterfs",
    "nfs",
    "nfs4",
    "smb3",
    "sshfs",
}

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
    ) -> None:
        self.request_id = request_id
        self.emit = emit
        self.clock = clock
        self.progress_interval = progress_interval
        self.entries = 0
        self.bytes = 0
        self.warning_count = 0
        self._last_progress = clock()

    def account(self, allocated_bytes: int) -> None:
        self.entries += 1
        self.bytes += allocated_bytes
        now = self.clock()
        if now - self._last_progress >= self.progress_interval:
            self.emit(
                {
                    "type": "progress",
                    "requestId": self.request_id,
                    "entries": self.entries,
                    "bytes": self.bytes,
                }
            )
            self._last_progress = now

    def warning(self, path: str, error: OSError | str) -> None:
        self.warning_count += 1
        if self.warning_count <= MAX_WARNINGS:
            self.emit(
                {
                    "type": "warning",
                    "requestId": self.request_id,
                    "path": path,
                    "error": str(error),
                }
            )


def allocated_size(stats: os.stat_result) -> int:
    blocks = getattr(stats, "st_blocks", None)
    return int(blocks * 512) if blocks is not None else int(stats.st_size)


def canonical_path(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def path_is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((canonical_path(path), canonical_path(parent))) == canonical_path(parent)
    except ValueError:
        return False


def descendant_mountpoints(
    mountpoint: str, findmnt_data: dict[str, Any]
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


def scan_directory(
    path: str,
    excluded_mounts: set[str],
    reporter: ScanReporter,
    cancellation: Cancellation,
) -> dict[str, Any]:
    """Measure a complete directory tree and return its directory records."""
    directories: list[dict[str, Any]] = []
    summary = scan_tree(
        path, excluded_mounts, reporter, cancellation, directories.append
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
) -> dict[str, Any]:
    """Scan once and emit finalized directory aggregates in post-order."""
    root = canonical_path(path)
    exclusions = {canonical_path(item) for item in excluded_mounts}
    seen_hardlinks: set[tuple[int, int]] = set()
    directory_count = 0

    def count_stat(stats: os.stat_result) -> int:
        if stat.S_ISREG(stats.st_mode) and stats.st_nlink > 1:
            key = (stats.st_dev, stats.st_ino)
            if key in seen_hardlinks:
                reporter.account(0)
                return 0
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

    stack = [
        open_frame(
            root,
            os.path.basename(root.rstrip(os.sep)) or root,
            None,
            count_stat(root_stats),
        )
    ]
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
            if is_excluded(entry_path, exclusions):
                continue
            try:
                stats = entry.stat(follow_symlinks=False)
            except OSError as error:
                reporter.warning(entry_path, error)
                continue

            if stat.S_ISDIR(stats.st_mode):
                stack.append(
                    open_frame(
                        entry_path,
                        entry.name,
                        frame["path"],
                        count_stat(stats),
                    )
                )
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


def run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def flatten_findmnt(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []

    def visit(entry: dict[str, Any]) -> None:
        flattened.append(entry)
        for child in entry.get("children") or []:
            visit(child)

    for entry in entries:
        visit(entry)
    return flattened


def index_block_devices(
    entries: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def visit(entry: dict[str, Any], physical: dict[str, Any] | None) -> None:
        current_physical = entry if entry.get("type") == "disk" else physical
        if current_physical is None:
            current_physical = entry

        pair = (entry, current_physical)
        for value in (entry.get("path"), entry.get("name"), entry.get("kname")):
            if value:
                index[str(value)] = pair
                if not str(value).startswith("/dev/"):
                    index["/dev/" + str(value)] = pair

        for child in entry.get("children") or []:
            visit(child, current_physical)

    for entry in entries:
        visit(entry, None)
    return index


def normalized_source(source: str) -> str:
    return source.split("[", 1)[0]


def capacity_for(path: str) -> dict[str, int | float]:
    stats = os.statvfs(path)
    fragment_size = stats.f_frsize or stats.f_bsize
    total = stats.f_blocks * fragment_size
    free = stats.f_bfree * fragment_size
    available = stats.f_bavail * fragment_size
    used = max(0, total - free)
    percent = (used * 100.0 / total) if total else 0.0
    return {
        "totalBytes": total,
        "usedBytes": used,
        "availableBytes": available,
        "usagePercent": round(percent, 1),
    }


def disk_name(device: dict[str, Any]) -> str:
    vendor = str(device.get("vendor") or "").strip()
    model = str(device.get("model") or "").strip()
    label = " ".join(part for part in (vendor, model) if part)
    return label or str(device.get("path") or device.get("name") or "Physical disk")


def filesystem_name(
    mountpoint: str,
    block: dict[str, Any] | None,
    source: str,
) -> str:
    label = str((block or {}).get("label") or "").strip()
    if label:
        return label
    if mountpoint == "/":
        return "Root filesystem"
    basename = os.path.basename(mountpoint.rstrip("/"))
    return basename or source or mountpoint


def is_network_filesystem(source: str, fstype: str) -> bool:
    lowered = fstype.lower()
    return (
        lowered in NETWORK_FILESYSTEMS
        or lowered.startswith("nfs")
        or lowered.startswith("fuse.sshfs")
        or source.startswith("//")
    )


def build_discovery(
    lsblk_data: dict[str, Any],
    findmnt_data: dict[str, Any],
    capacity_provider: Callable[[str], dict[str, int | float]] = capacity_for,
) -> dict[str, Any]:
    block_index = index_block_devices(lsblk_data.get("blockdevices") or [])
    mounts = flatten_findmnt(findmnt_data.get("filesystems") or [])
    filesystems: list[dict[str, Any]] = []
    seen_targets: set[str] = set()

    for mount in mounts:
        mountpoint = str(mount.get("target") or "")
        source = str(mount.get("source") or "")
        fstype = str(mount.get("fstype") or "")
        if not mountpoint or mountpoint in seen_targets:
            continue
        if fstype.lower() in PSEUDO_FILESYSTEMS:
            continue
        seen_targets.add(mountpoint)

        source_device = normalized_source(source)
        pair = block_index.get(source_device)
        block = pair[0] if pair else None
        physical = pair[1] if pair else None
        network = is_network_filesystem(source, fstype)

        if network:
            group_id = "network"
            group_name = "Network filesystems"
            removable = False
        elif physical:
            group_id = str(physical.get("path") or physical.get("name") or "other")
            group_name = disk_name(physical)
            removable = bool(physical.get("rm"))
        else:
            group_id = "other"
            group_name = "Other filesystems"
            removable = False

        try:
            capacity = capacity_provider(mountpoint)
        except OSError as error:
            capacity = {
                "totalBytes": 0,
                "usedBytes": 0,
                "availableBytes": 0,
                "usagePercent": 0.0,
            }
            capacity["capacityError"] = str(error)

        filesystems.append(
            {
                "id": mountpoint,
                "displayName": filesystem_name(mountpoint, block, source),
                "device": source,
                "devicePath": source_device,
                "mountpoint": mountpoint,
                "filesystemType": fstype,
                "filesystemLabel": str((block or {}).get("label") or ""),
                "uuid": str((block or {}).get("uuid") or ""),
                "groupId": group_id,
                "groupName": group_name,
                "physicalDevice": str((physical or {}).get("path") or ""),
                "physicalDiskLabel": disk_name(physical) if physical else "",
                "transport": str((physical or {}).get("tran") or ""),
                "removable": removable,
                "network": network,
                **capacity,
            }
        )

    filesystems.sort(
        key=lambda item: (
            item["groupName"].casefold(),
            item["mountpoint"] != "/",
            item["mountpoint"].casefold(),
        )
    )
    return {"schemaVersion": 1, "filesystems": filesystems}


def discover() -> dict[str, Any]:
    lsblk_data = run_json(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--output",
            "NAME,KNAME,PATH,TYPE,PKNAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL,VENDOR,TRAN,RM",
        ]
    )
    findmnt_data = run_json(
        [
            "findmnt",
            "--json",
            "--bytes",
            "--output",
            "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN",
        ]
    )
    return build_discovery(lsblk_data, findmnt_data)


def emit_ndjson(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def scan_command(args: argparse.Namespace) -> int:
    mountpoint = canonical_path(args.mountpoint)
    path = canonical_path(args.path)
    request_id = args.request_id

    if not path_is_within(path, mountpoint):
        emit_ndjson(
            {
                "type": "error",
                "requestId": request_id,
                "path": path,
                "error": "scan path is outside the selected filesystem mountpoint",
            }
        )
        return 2
    try:
        path_stats = os.stat(path, follow_symlinks=False)
    except OSError:
        path_stats = None
    if path_stats is None or not stat.S_ISDIR(path_stats.st_mode):
        emit_ndjson(
            {
                "type": "error",
                "requestId": request_id,
                "path": path,
                "error": "scan path is not an accessible directory",
            }
        )
        return 2
    if os.path.realpath(path) != path:
        emit_ndjson(
            {
                "type": "error",
                "requestId": request_id,
                "path": path,
                "error": "scan path may not contain symbolic-link components",
            }
        )
        return 2

    findmnt_data = run_json(
        [
            "findmnt",
            "--json",
            "--bytes",
            "--output",
            "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN",
        ]
    )
    exclusions = descendant_mountpoints(mountpoint, findmnt_data)
    if is_excluded(path, exclusions):
        emit_ndjson(
            {
                "type": "error",
                "requestId": request_id,
                "path": path,
                "error": "scan path belongs to a descendant mounted filesystem",
            }
        )
        return 2

    cancellation = Cancellation()
    signal.signal(signal.SIGTERM, cancellation.cancel)
    signal.signal(signal.SIGINT, cancellation.cancel)
    reporter = ScanReporter(request_id, emit_ndjson)
    started = time.monotonic()
    emit_ndjson(
        {
            "type": "start",
            "requestId": request_id,
            "path": path,
            "mountpoint": mountpoint,
            "excludedMountpoints": sorted(exclusions),
        }
    )

    try:
        result = scan_tree(
            path,
            exclusions,
            reporter,
            cancellation,
            lambda record: emit_ndjson(
                {"type": "directory", "requestId": request_id, **record}
            ),
        )
        cancellation.check()
    except ScanCancelled:
        emit_ndjson(
            {
                "type": "cancelled",
                "requestId": request_id,
                "path": path,
                "entries": reporter.entries,
                "bytes": reporter.bytes,
                "durationMs": round((time.monotonic() - started) * 1000),
            }
        )
        return 130

    emit_ndjson(
        {
            "type": "complete",
            "requestId": request_id,
            "path": path,
            "bytes": result["bytes"],
            "directFilesBytes": result["directFilesBytes"],
            "entries": reporter.entries,
            "directoryCount": result["directoryCount"],
            "warningCount": reporter.warning_count,
            "suppressedWarningCount": max(0, reporter.warning_count - MAX_WARNINGS),
            "durationMs": round((time.monotonic() - started) * 1000),
        }
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OmaTree filesystem helper")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("discover", help="emit mounted filesystems as JSON")
    scan_parser = subparsers.add_parser("scan", help="scan one filesystem tree")
    scan_parser.add_argument("--mountpoint", required=True)
    scan_parser.add_argument("--path", required=True)
    scan_parser.add_argument("--request-id", required=True)
    args = parser.parse_args()

    if args.command == "scan":
        try:
            return scan_command(args)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            emit_ndjson(
                {
                    "type": "error",
                    "requestId": args.request_id,
                    "path": canonical_path(args.path),
                    "error": str(error),
                }
            )
            return 1

    try:
        json.dump(discover(), sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        json.dump({"schemaVersion": 1, "error": str(error)}, sys.stderr)
        sys.stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
