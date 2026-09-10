"""Stable JSON event contract for streamed OmaTree scans."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


PROTOCOL_VERSION = 1

START = "start"
PROGRESS = "progress"
WARNING = "warning"
DIRECTORY = "directory"
COMPLETE = "complete"
CANCELLED = "cancelled"
ERROR = "error"

EVENT_TYPES = frozenset({
    START, PROGRESS, WARNING, DIRECTORY, COMPLETE, CANCELLED, ERROR,
})
TERMINAL_EVENT_TYPES = frozenset({COMPLETE, CANCELLED, ERROR})


def start_event(
    request_id: str,
    path: str,
    mountpoint: str,
    excluded_mountpoints: list[str],
) -> dict[str, Any]:
    return {
        "type": START,
        "requestId": request_id,
        "path": path,
        "mountpoint": mountpoint,
        "excludedMountpoints": excluded_mountpoints,
    }


def progress_event(request_id: str, entries: int, bytes_: int) -> dict[str, Any]:
    return {
        "type": PROGRESS,
        "requestId": request_id,
        "entries": entries,
        "bytes": bytes_,
    }


def warning_event(request_id: str, path: str, error: object) -> dict[str, Any]:
    return {
        "type": WARNING,
        "requestId": request_id,
        "path": path,
        "error": str(error),
    }


def directory_event(
    request_id: str, record: Mapping[str, Any],
) -> dict[str, Any]:
    return {"type": DIRECTORY, "requestId": request_id, **record}


def complete_event(
    request_id: str,
    path: str,
    bytes_: int,
    direct_files_bytes: int,
    entries: int,
    directory_count: int,
    warning_count: int,
    suppressed_warning_count: int,
    duration_ms: int,
) -> dict[str, Any]:
    return {
        "type": COMPLETE,
        "requestId": request_id,
        "path": path,
        "bytes": bytes_,
        "directFilesBytes": direct_files_bytes,
        "entries": entries,
        "directoryCount": directory_count,
        "warningCount": warning_count,
        "suppressedWarningCount": suppressed_warning_count,
        "durationMs": duration_ms,
    }


def cancelled_event(
    request_id: str,
    path: str,
    entries: int,
    bytes_: int,
    duration_ms: int,
) -> dict[str, Any]:
    return {
        "type": CANCELLED,
        "requestId": request_id,
        "path": path,
        "entries": entries,
        "bytes": bytes_,
        "durationMs": duration_ms,
    }


def error_event(request_id: str, path: str, error: object) -> dict[str, Any]:
    return {
        "type": ERROR,
        "requestId": request_id,
        "path": path,
        "error": str(error),
    }


def encode_ndjson(event: Mapping[str, Any]) -> str:
    """Encode one event using the helper's existing compact NDJSON format."""
    return json.dumps(event, separators=(",", ":")) + "\n"
