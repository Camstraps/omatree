"""Terminal-independent controllers for the OmaTree TUI."""

from __future__ import annotations

from dataclasses import dataclass
import os
import threading
from typing import Any, Callable

from helper.omatree_core import discovery, scanner
from helper.omatree_core.paths import (
    canonical_path,
    descendant_mountpoints,
    is_excluded,
    path_is_within,
    validate_scan_root,
)
from helper.omatree_core.snapshot import Snapshot, SnapshotBuilder


FINDMNT_COMMAND = [
    "/usr/bin/findmnt",
    "--json",
    "--bytes",
    "--output",
    "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN",
]


class TargetError(ValueError):
    """Raised when a requested scan target is invalid or cannot be resolved."""


@dataclass(frozen=True, slots=True)
class ScanPlan:
    path: str
    mountpoint: str
    excluded_mountpoints: frozenset[str]


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    snapshot: Snapshot | None
    cancelled: bool = False
    error: str = ""


def requested_path(argument: str | None, environ: dict[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    raw_path = argument if argument is not None else environment.get("HOME")
    if not raw_path:
        raw_path = os.path.expanduser("~")
    return canonical_path(os.path.expanduser(raw_path))


def containing_filesystem(
    path: str, filesystems: list[dict[str, Any]],
) -> dict[str, Any] | None:
    matches = [
        filesystem for filesystem in filesystems
        if filesystem.get("mountpoint")
        and path_is_within(path, str(filesystem["mountpoint"]))
    ]
    if not matches:
        return None
    return max(matches, key=lambda filesystem: len(canonical_path(
        str(filesystem["mountpoint"])
    )))


def make_scan_plan(
    argument: str | None,
    environ: dict[str, str] | None = None,
    discover_provider: Callable[[], dict[str, Any]] = discovery.discover,
    findmnt_provider: Callable[[list[str]], dict[str, Any]] = discovery.run_json,
) -> ScanPlan:
    path = requested_path(argument, environ)
    discovered = discover_provider()
    filesystems = discovered.get("filesystems") if isinstance(discovered, dict) else None
    if not isinstance(filesystems, list):
        raise TargetError("Filesystem discovery returned an invalid response.")
    filesystem = containing_filesystem(path, filesystems)
    if filesystem is None:
        raise TargetError("No mounted filesystem contains the requested path.")
    mountpoint = canonical_path(str(filesystem["mountpoint"]))
    validation_error = validate_scan_root(path, mountpoint)
    if validation_error is not None:
        raise TargetError(validation_error)

    findmnt_data = findmnt_provider(FINDMNT_COMMAND)
    exclusions = descendant_mountpoints(mountpoint, findmnt_data)
    if is_excluded(path, exclusions):
        raise TargetError("scan path belongs to a descendant mounted filesystem")
    return ScanPlan(path, mountpoint, frozenset(exclusions))


class ScanController:
    """Run one core scan at a time and replace snapshots only on success."""

    def __init__(
        self,
        plan: ScanPlan,
        scan_function: Callable[..., dict[str, Any]] = scanner.scan_tree,
    ) -> None:
        self.plan = plan
        self._scan_function = scan_function
        self._active_snapshot: Snapshot | None = None
        self._cancellation: scanner.Cancellation | None = None
        self._lock = threading.Lock()

    @property
    def active_snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._active_snapshot

    def cancel(self) -> None:
        with self._lock:
            cancellation = self._cancellation
        if cancellation is not None:
            cancellation.cancel()

    def scan(
        self,
        emit: Callable[[dict[str, Any]], None] | None = None,
        cancellation: scanner.Cancellation | None = None,
    ) -> ScanOutcome:
        event_sink = emit or (lambda _event: None)
        current_cancellation = cancellation or scanner.Cancellation()
        builder = SnapshotBuilder()
        reporter = scanner.ScanReporter("tui", event_sink)
        with self._lock:
            self._cancellation = current_cancellation
        try:
            summary = self._scan_function(
                self.plan.path,
                set(self.plan.excluded_mountpoints),
                reporter,
                current_cancellation,
                builder.add_directory,
            )
            current_cancellation.check()
            completed = builder.finalize(
                self.plan.path, int(summary["directoryCount"])
            )
        except scanner.ScanCancelled:
            return ScanOutcome(None, cancelled=True)
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            return ScanOutcome(None, error=str(error))
        finally:
            with self._lock:
                if self._cancellation is current_cancellation:
                    self._cancellation = None

        with self._lock:
            self._active_snapshot = completed
        return ScanOutcome(completed)


class BrowserController:
    """Expansion, cursor, and visible-row state for the terminal frontend."""

    def __init__(self, snapshot: Snapshot | None = None) -> None:
        self.snapshot: Snapshot | None = None
        self.expanded: set[str] = set()
        self.cursor = 0
        self.scroll = 0
        self._visible_paths: list[str] = []
        if snapshot is not None:
            self.set_snapshot(snapshot)

    @property
    def visible_paths(self) -> list[str]:
        return list(self._visible_paths)

    @property
    def selected_path(self) -> str | None:
        if not self._visible_paths:
            return None
        return self._visible_paths[self.cursor]

    def set_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.expanded = {snapshot.root_path}
        self.cursor = 0
        self.scroll = 0
        self._rebuild_visible()

    def _rebuild_visible(self) -> None:
        if self.snapshot is None:
            self._visible_paths = []
            self.cursor = 0
            self.scroll = 0
            return
        visible: list[str] = []
        stack = [self.snapshot.root_path]
        while stack:
            path = stack.pop()
            visible.append(path)
            if path in self.expanded:
                stack.extend(reversed(self.snapshot.directories[path].children))
        self._visible_paths = visible
        self.cursor = max(0, min(self.cursor, len(visible) - 1))
        self.scroll = max(0, min(self.scroll, self.cursor))

    def move(self, delta: int) -> None:
        if self._visible_paths:
            self.cursor = max(0, min(len(self._visible_paths) - 1, self.cursor + delta))

    def first(self) -> None:
        self.cursor = 0

    def last(self) -> None:
        if self._visible_paths:
            self.cursor = len(self._visible_paths) - 1

    def page(self, delta: int, page_size: int) -> None:
        self.move(delta * max(1, page_size))

    def expand_selected(self) -> None:
        path = self.selected_path
        if path is None or self.snapshot is None:
            return
        if self.snapshot.directories[path].children:
            self.expanded.add(path)
            self._rebuild_visible()

    def collapse_or_parent(self) -> None:
        path = self.selected_path
        if path is None or self.snapshot is None:
            return
        node = self.snapshot.directories[path]
        if path in self.expanded:
            self.expanded.remove(path)
            self._rebuild_visible()
            return
        if node.parent_path:
            self.select_path(node.parent_path)

    def select_parent(self) -> None:
        path = self.selected_path
        if path is None or self.snapshot is None:
            return
        parent = self.snapshot.directories[path].parent_path
        if parent:
            self.select_path(parent)

    def select_path(self, path: str) -> None:
        if path in self._visible_paths:
            self.cursor = self._visible_paths.index(path)

    def keep_cursor_visible(self, page_size: int) -> None:
        page_size = max(1, page_size)
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + page_size:
            self.scroll = self.cursor - page_size + 1
        maximum = max(0, len(self._visible_paths) - page_size)
        self.scroll = max(0, min(self.scroll, maximum))
