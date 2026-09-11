"""Frontend-neutral, directory-only snapshot semantics for OmaTree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import protocol
from .resources import DEFAULT_LIMITS, ResourceLimitExceeded, ResourceLimits, check_path


class SnapshotError(ValueError):
    """Raised when a stream cannot produce an atomically valid snapshot."""


@dataclass(slots=True)
class Directory:
    name: str
    path: str
    bytes: int
    direct_files_bytes: int
    child_directory_count: int
    warning_count: int
    parent_path: str
    depth: int = 0
    children: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchResult:
    matches: list[Directory]
    total: int


class Snapshot:
    def __init__(self, directories: dict[str, Directory], root_path: str) -> None:
        self.directories = directories
        self.root_path = root_path

    @property
    def root(self) -> Directory:
        return self.directories[self.root_path]

    def percentage_of_parent(self, path: str, root_bytes: int = 0) -> float:
        node = self.directories.get(path)
        if node is None:
            return 0.0
        if not node.parent_path:
            return 100.0 if node.bytes > 0 else 0.0
        parent = self.directories.get(node.parent_path)
        parent_bytes = parent.bytes if parent is not None else (root_bytes or node.bytes)
        if parent_bytes <= 0:
            return 0.0
        return max(0.0, min(100.0, node.bytes * 100.0 / parent_bytes))

    def ancestor_paths(self, path: str) -> list[str]:
        result: list[str] = []
        current = self.directories.get(path)
        seen: set[str] = set()
        while current is not None and current.path not in seen:
            result.insert(0, current.path)
            seen.add(current.path)
            current = self.directories.get(current.parent_path)
        return result

    def search(self, query: str, limit: int | None = None) -> SearchResult:
        normalized = str(query or "").lower()
        if not normalized:
            return SearchResult([], 0)
        matches = [
            node for node in self.directories.values()
            if normalized in node.name.lower()
        ]
        matches.sort(key=lambda node: (-node.bytes, node.path))
        total = len(matches)
        if limit is not None:
            matches = matches[:max(0, limit)]
        return SearchResult(matches, total)


class SnapshotBuilder:
    """Stage records privately and publish a Snapshot only after validation."""

    def __init__(self, limits: ResourceLimits = DEFAULT_LIMITS) -> None:
        self._limits = limits
        self._directories: dict[str, Directory] = {}
        self._pending_children: dict[str, list[str]] = {}

    def add_directory(self, record: Mapping[str, Any]) -> None:
        path = str(record.get("path") or "")
        if not path or path in self._directories:
            raise SnapshotError("Scanner returned duplicate or empty directory paths.")
        parent_value = record.get("parentPath")
        parent_path = "" if parent_value is None else str(parent_value or "")
        check_path(path, self._limits)
        if parent_path:
            check_path(parent_path, self._limits)
        if len(self._directories) >= self._limits.max_directories:
            raise ResourceLimitExceeded(
                "directories", self._limits.max_directories
            )
        node = Directory(
            name=str(record.get("name") or path),
            path=path,
            bytes=int(record.get("bytes") or 0),
            direct_files_bytes=int(record.get("directFilesBytes") or 0),
            child_directory_count=int(record.get("childDirectoryCount") or 0),
            warning_count=int(record.get("warningCount") or 0),
            parent_path=parent_path,
            children=self._pending_children.pop(path, []),
        )
        self._directories[path] = node
        if parent_path:
            parent = self._directories.get(parent_path)
            if parent is not None:
                parent.children.append(path)
            else:
                self._pending_children.setdefault(parent_path, []).append(path)

    def finalize(
        self, root_path: str, record_count: int, root_name: str | None = None,
    ) -> Snapshot:
        root = self._directories.get(root_path)
        if root is None:
            raise SnapshotError("Scanner did not return the filesystem root directory.")
        if root_name:
            root.name = str(root_name)
        root.parent_path = ""

        if self._pending_children:
            unresolved = next(iter(self._pending_children))
            raise SnapshotError(
                "Scanner returned a directory with no parent: " + unresolved
            )

        visited: set[str] = set()
        stack = [(root_path, 0)]
        while stack:
            path, depth = stack.pop()
            if path in visited:
                raise SnapshotError("Scanner returned a cyclic directory hierarchy.")
            visited.add(path)
            current = self._directories[path]
            current.depth = depth
            if len(current.children) != current.child_directory_count:
                raise SnapshotError(
                    "Scanner returned an inconsistent directory hierarchy."
                )
            current.children.sort(key=lambda child_path: (
                -self._directories[child_path].bytes,
                self._directories[child_path].name.lower(),
            ))
            for child_path in reversed(current.children):
                stack.append((child_path, depth + 1))

        if len(visited) != record_count or len(visited) != len(self._directories):
            raise SnapshotError(
                "Scanner returned directories outside the filesystem root."
            )
        return Snapshot(self._directories, root_path)


def snapshot_from_events(
    events: Iterable[Mapping[str, Any]],
    limits: ResourceLimits = DEFAULT_LIMITS,
) -> Snapshot:
    """Atomically build a snapshot from one complete, well-formed scan stream."""
    builder = SnapshotBuilder(limits)
    request_id: str | None = None
    root_path: str | None = None
    directory_count = 0
    terminal_type: str | None = None
    complete: Mapping[str, Any] | None = None

    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise SnapshotError("Malformed scan event.")
        event_type = event.get("type")
        event_request_id = event.get("requestId")
        if event_type not in protocol.EVENT_TYPES or not isinstance(event_request_id, str):
            raise SnapshotError("Malformed scan event.")
        if index == 0:
            if event_type != protocol.START:
                raise SnapshotError("Scan stream did not begin with start.")
            request_id = event_request_id
            root_path = str(event.get("path") or "")
        elif event_request_id != request_id:
            raise SnapshotError("Scan stream contains mismatched request IDs.")
        if terminal_type is not None:
            raise SnapshotError("Scan stream contains events after its terminal event.")

        if event_type == protocol.DIRECTORY:
            builder.add_directory(event)
            directory_count += 1
        elif event_type in protocol.TERMINAL_EVENT_TYPES:
            terminal_type = str(event_type)
            if event_type == protocol.COMPLETE:
                complete = event

    if terminal_type != protocol.COMPLETE or complete is None:
        raise SnapshotError("Scan stream did not complete successfully.")
    if not root_path:
        raise SnapshotError("Scanner did not return the filesystem root directory.")
    expected_count = int(complete.get("directoryCount") or 0)
    if expected_count != directory_count:
        raise SnapshotError("Scanner returned an incomplete directory tree.")
    return builder.finalize(root_path, directory_count)
