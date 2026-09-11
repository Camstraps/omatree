"""Fixed resource ceilings shared by OmaTree frontends and helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_directories: int = 500_000
    max_entries: int = 20_000_000
    max_hardlink_identities: int = 250_000
    max_path_bytes: int = 4096
    max_event_bytes: int = 64 * 1024
    max_total_ndjson_bytes: int = 512 * 1024 * 1024
    max_discovered_filesystems: int = 4096

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_total_ndjson_bytes < self.max_event_bytes:
            raise ValueError(
                "max_total_ndjson_bytes must reserve at least one event"
            )


DEFAULT_LIMITS = ResourceLimits()


class ResourceLimitExceeded(RuntimeError):
    """Raised before an OmaTree resource ceiling would be exceeded."""

    def __init__(self, resource: str, maximum: int) -> None:
        self.resource = resource
        self.maximum = maximum
        super().__init__(
            f"Resource limit exceeded: {resource} (maximum {maximum})"
        )


def encoded_path_size(path: str) -> int:
    return len(path.encode("utf-8", errors="surrogateescape"))


def check_path(path: str, limits: ResourceLimits) -> None:
    if encoded_path_size(path) > limits.max_path_bytes:
        raise ResourceLimitExceeded("path bytes", limits.max_path_bytes)


class NdjsonBudget:
    """Account encoded events while reserving room for one terminal error."""

    def __init__(self, limits: ResourceLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits
        self.total_bytes = 0

    def account(self, encoded: str, terminal: bool = False) -> None:
        size = len(encoded.encode("utf-8"))
        if size > self.limits.max_event_bytes:
            raise ResourceLimitExceeded(
                "encoded event bytes", self.limits.max_event_bytes
            )
        ceiling = self.limits.max_total_ndjson_bytes
        if not terminal:
            ceiling -= self.limits.max_event_bytes
        if self.total_bytes + size > ceiling:
            raise ResourceLimitExceeded(
                "total NDJSON bytes", self.limits.max_total_ndjson_bytes
            )
        self.total_bytes += size
