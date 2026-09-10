#!/usr/bin/env python3
"""Discover user-relevant mounted filesystems for OmaTree."""

from __future__ import annotations

import json
import argparse
import signal
import subprocess
import sys
import time

from omatree_core.discovery import (
    build_discovery,
    discover,
    run_json,
)
from omatree_core.paths import (
    canonical_path,
    descendant_mountpoints,
    is_excluded,
    path_is_within,
    validate_scan_root,
)
from omatree_core.scanner import (
    MAX_WARNINGS,
    Cancellation,
    ScanCancelled,
    ScanReporter,
    allocated_size,
    scan_directory,
    scan_tree,
)


def emit_ndjson(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def scan_command(args: argparse.Namespace) -> int:
    mountpoint = canonical_path(args.mountpoint)
    path = canonical_path(args.path)
    request_id = args.request_id

    validation_error = validate_scan_root(path, mountpoint)
    if validation_error is not None:
        emit_ndjson(
            {
                "type": "error",
                "requestId": request_id,
                "path": path,
                "error": validation_error,
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
