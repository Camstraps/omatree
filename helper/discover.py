#!/usr/bin/python3
"""Discover user-relevant mounted filesystems for OmaTree."""

from __future__ import annotations

import json
import argparse
import signal
import subprocess
import sys
import time
from typing import Any

from omatree_core import protocol
from omatree_core.discovery import (
    DISCOVERY_STDOUT_LIMIT,
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
from omatree_core.resources import (
    DEFAULT_LIMITS,
    NdjsonBudget,
    ResourceLimitExceeded,
    ResourceLimits,
)


def emit_ndjson(message: dict[str, Any]) -> None:
    sys.stdout.write(protocol.encode_ndjson(message))
    sys.stdout.flush()


def emit_bounded_error(
    request_id: str,
    path: str,
    error: object,
    limits: ResourceLimits = DEFAULT_LIMITS,
) -> None:
    budget = NdjsonBudget(limits)
    message = protocol.error_event(request_id, path, error)
    try:
        budget.account(protocol.encode_ndjson(message), terminal=True)
    except ResourceLimitExceeded:
        message = protocol.error_event(request_id, "", error)
        budget.account(protocol.encode_ndjson(message), terminal=True)
    emit_ndjson(message)


def scan_command(
    args: argparse.Namespace, limits: ResourceLimits = DEFAULT_LIMITS,
) -> int:
    mountpoint = canonical_path(args.mountpoint)
    path = canonical_path(args.path)
    request_id = args.request_id
    budget = NdjsonBudget(limits)

    def emit_event(message: dict[str, Any], terminal: bool = False) -> None:
        budget.account(protocol.encode_ndjson(message), terminal=terminal)
        emit_ndjson(message)

    def emit_resource_failure(error: ResourceLimitExceeded) -> None:
        message = protocol.error_event(request_id, path, error)
        try:
            emit_event(message, terminal=True)
        except ResourceLimitExceeded:
            # An oversized caller-supplied path must not consume the reserved
            # terminal-event budget or bypass the NDJSON ceiling.
            emit_event(
                protocol.error_event(request_id, "", error), terminal=True
            )

    validation_error = validate_scan_root(path, mountpoint)
    if validation_error is not None:
        try:
            emit_event(
                protocol.error_event(request_id, path, validation_error),
                terminal=True,
            )
        except ResourceLimitExceeded as error:
            emit_resource_failure(error)
            return 3
        return 2

    findmnt_data = run_json(
        [
            "/usr/bin/findmnt",
            "--json",
            "--bytes",
            "--output",
            "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN",
        ]
    )
    exclusions = descendant_mountpoints(mountpoint, findmnt_data)
    if is_excluded(path, exclusions):
        try:
            emit_event(
                protocol.error_event(
                    request_id,
                    path,
                    "scan path belongs to a descendant mounted filesystem",
                ),
                terminal=True,
            )
        except ResourceLimitExceeded as error:
            emit_resource_failure(error)
            return 3
        return 2

    cancellation = Cancellation()
    signal.signal(signal.SIGTERM, cancellation.cancel)
    signal.signal(signal.SIGINT, cancellation.cancel)
    reporter = ScanReporter(request_id, emit_event, limits=limits)
    started = time.monotonic()

    try:
        emit_event(protocol.start_event(
            request_id, path, mountpoint, sorted(exclusions)
        ))
        result = scan_tree(
            path,
            exclusions,
            reporter,
            cancellation,
            lambda record: emit_event(protocol.directory_event(request_id, record)),
            limits,
        )
        cancellation.check()
    except ScanCancelled:
        try:
            emit_event(protocol.cancelled_event(
                request_id,
                path,
                reporter.entries,
                reporter.bytes,
                round((time.monotonic() - started) * 1000),
            ), terminal=True)
        except ResourceLimitExceeded as error:
            emit_resource_failure(error)
            return 3
        return 130
    except ResourceLimitExceeded as error:
        emit_resource_failure(error)
        return 3

    try:
        emit_event(protocol.complete_event(
            request_id,
            path,
            result["bytes"],
            result["directFilesBytes"],
            reporter.entries,
            result["directoryCount"],
            reporter.warning_count,
            max(0, reporter.warning_count - MAX_WARNINGS),
            round((time.monotonic() - started) * 1000),
        ), terminal=True)
    except ResourceLimitExceeded as error:
        emit_resource_failure(error)
        return 3
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
        except (
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            ResourceLimitExceeded,
        ) as error:
            emit_bounded_error(
                args.request_id, canonical_path(args.path), error
            )
            return 1

    try:
        encoded = json.dumps(discover(), separators=(",", ":")) + "\n"
        if len(encoded.encode("utf-8")) > DISCOVERY_STDOUT_LIMIT:
            raise ResourceLimitExceeded(
                "discovery output bytes", DISCOVERY_STDOUT_LIMIT
            )
        sys.stdout.write(encoded)
        return 0
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        ResourceLimitExceeded,
    ) as error:
        json.dump({"schemaVersion": 1, "error": str(error)}, sys.stderr)
        sys.stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
