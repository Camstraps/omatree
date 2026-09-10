"""Mounted-filesystem discovery and capacity reporting for OmaTree."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable


PSEUDO_FILESYSTEMS = {
    "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs",
    "debugfs", "devpts", "devtmpfs", "efivarfs", "fusectl",
    "fuse.gvfsd-fuse", "fuse.portal", "hugetlbfs", "mqueue", "proc",
    "pstore", "ramfs", "securityfs", "sysfs", "tmpfs", "tracefs",
}

NETWORK_FILESYSTEMS = {
    "9p", "afs", "ceph", "cifs", "davfs", "davfs2", "fuse.sshfs",
    "glusterfs", "nfs", "nfs4", "smb3", "sshfs",
}


def run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
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
    mountpoint: str, block: dict[str, Any] | None, source: str,
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

        filesystems.append({
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
        })

    filesystems.sort(key=lambda item: (
        item["groupName"].casefold(),
        item["mountpoint"] != "/",
        item["mountpoint"].casefold(),
    ))
    return {"schemaVersion": 1, "filesystems": filesystems}


def discover() -> dict[str, Any]:
    lsblk_data = run_json([
        "lsblk", "--json", "--bytes", "--output",
        "NAME,KNAME,PATH,TYPE,PKNAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL,VENDOR,TRAN,RM",
    ])
    findmnt_data = run_json([
        "findmnt", "--json", "--bytes", "--output",
        "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN",
    ])
    return build_discovery(lsblk_data, findmnt_data)
