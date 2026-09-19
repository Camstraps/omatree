"""Persistent, bounded control plane for disk-backed OmaTree snapshots."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import json
import inspect
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import stat
import threading
import time
from typing import Any, BinaryIO, Callable, Mapping

from . import protocol as scan_protocol
from .broker_protocol import (
    BROKER_PROTOCOL_VERSION,
    BrokerProtocolError,
    BrokerRequest,
    BrokerStreamError,
    MAX_CONTINUATION_BYTES,
    MAX_GENERATION_ID_BYTES,
    MAX_INPUT_BUFFER_BYTES,
    MAX_OUTSTANDING_REQUESTS,
    MAX_REQUEST_LINE_BYTES,
    QUERY_OPERATIONS,
    encode_message,
    parse_request_line,
    validate_identifier,
)
from .discovery import FINDMNT_PATH, run_json
from .paths import canonical_path, descendant_mountpoints, is_excluded, validate_scan_root
from .resources import DEFAULT_LIMITS, ResourceLimitExceeded, ResourceLimits, check_path
from .scanner import MAX_WARNINGS, Cancellation, ScanCancelled, ScanReporter, scan_tree
from .sqlite_query import (
    DEFAULT_QUERY_LIMITS,
    QueryLimits,
    SQLiteQueryError,
    SQLiteSnapshotReader,
    encode_query_page,
)
from .sqlite_snapshot import (
    DEFAULT_SQLITE_LIMITS,
    SQLiteSnapshot,
    SQLiteSnapshotLimits,
    SQLiteSnapshotError, SQLiteSnapshotWriter,
    ensure_snapshot_runtime_dir,
)
from .persistent_snapshots import PersistentSnapshotStore


SCAN_TIMEOUT_SECONDS = 60 * 60
QUERY_TIMEOUT_SECONDS = 2.0
SEARCH_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 2.0
PROGRESS_INTERVAL_SECONDS = 0.25


@dataclass(slots=True)
class BrokerHighWater:
    outstanding_requests: int = 0
    scan_workers: int = 0
    search_workers: int = 0
    active_generations: int = 0
    staging_generations: int = 0
    output_messages: int = 0
    output_bytes: int = 0


@dataclass(slots=True)
class _Generation:
    generation_id: str
    snapshot: SQLiteSnapshot
    references: int = 0
    retired: bool = False
    mountpoint: str = ""
    persistent: bool = False


@dataclass(slots=True)
class _ScanState:
    generation_id: str
    request_id: str
    token: str
    cancellation: Cancellation
    timeout: bool = False
    cancel_requested: bool = False
    terminal_emitted: bool = False
    writer: SQLiteSnapshotWriter | None = None
    snapshot: SQLiteSnapshot | None = None
    thread: threading.Thread | None = None
    timer: threading.Timer | None = None
    last_progress_emit: float = 0.0
    warnings_emitted: int = 0


@dataclass(slots=True)
class _QueryJob:
    request: BrokerRequest
    generation: _Generation
    superseded: threading.Event


class BoundedOutputWriter:
    """Synchronous writes: the pipe backpressures producers without a queue."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.closed = False
        self.messages_written = 0
        self.max_message_bytes = 0

    def write(self, message: Mapping[str, Any], *, query: bool = False) -> None:
        encoded = encode_message(message, query=query)
        with self._lock:
            if self.closed:
                raise BrokenPipeError("broker output is closed")
            self._stream.write(encoded)
            self._stream.flush()
            self.messages_written += 1
            self.max_message_bytes = max(self.max_message_bytes, len(encoded))

    def close(self) -> None:
        with self._lock:
            self.closed = True


def create_broker_session(
    runtime_dir: str | os.PathLike[str] | None = None,
) -> Path:
    root = ensure_snapshot_runtime_dir(runtime_dir)
    for _attempt in range(16):
        session = root / ("session-" + secrets.token_hex(16))
        try:
            session.mkdir(mode=0o700)
        except FileExistsError:
            continue
        details = os.lstat(session)
        if (
            not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
            or Path(os.path.realpath(session)) != session
        ):
            try:
                session.rmdir()
            except OSError:
                pass
            raise BrokerProtocolError("broker session directory is unsafe")
        return session
    raise BrokerProtocolError("could not allocate broker session")


class SnapshotBroker:
    """Own active/staging generations and serialize bounded snapshot queries."""

    def __init__(
        self,
        output: BoundedOutputWriter,
        *,
        runtime_dir: str | os.PathLike[str] | None = None,
        resource_limits: ResourceLimits = DEFAULT_LIMITS,
        query_limits: QueryLimits = DEFAULT_QUERY_LIMITS,
        sqlite_limits: SQLiteSnapshotLimits = DEFAULT_SQLITE_LIMITS,
        scan_function: Callable[..., dict[str, Any]] = scan_tree,
        exclusion_provider: Callable[[str], set[str]] | None = None,
        scan_timeout: float = SCAN_TIMEOUT_SECONDS,
        query_timeout: float = QUERY_TIMEOUT_SECONDS,
        search_timeout: float = SEARCH_TIMEOUT_SECONDS,
        cache_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.output = output
        self.session_dir = create_broker_session(runtime_dir)
        self.resource_limits = resource_limits
        self.query_limits = query_limits
        self.sqlite_limits = sqlite_limits
        self.scan_function = scan_function
        self.exclusion_provider = exclusion_provider or self._discover_exclusions
        self.scan_timeout = scan_timeout
        self.query_timeout = query_timeout
        self.search_timeout = search_timeout
        if cache_dir is None and runtime_dir is not None:
            cache_dir = Path(runtime_dir) / "persistent-test-cache"
        self._cache_dir = cache_dir
        self.snapshot_store: PersistentSnapshotStore | None = None
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active: _Generation | None = None
        self._previous: _Generation | None = None
        self._staging: _ScanState | None = None
        self._query_jobs: deque[_QueryJob] = deque()
        self._outstanding: dict[str, _QueryJob] = {}
        self._current_query: _QueryJob | None = None
        self._current_reader: SQLiteSnapshotReader | None = None
        self._recent_ids: deque[str] = deque()
        self._recent_id_set: set[str] = set()
        self._shutdown = False
        self.high_water = BrokerHighWater()
        self._query_thread = threading.Thread(
            target=self._query_loop, name="omatree-query", daemon=True
        )
        self._query_thread.start()

    @staticmethod
    def _discover_exclusions(mountpoint: str) -> set[str]:
        data = run_json([
            FINDMNT_PATH, "--json", "--bytes", "--output",
            "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN",
        ])
        return descendant_mountpoints(mountpoint, data)

    @property
    def active_generation_id(self) -> str | None:
        with self._lock:
            return self._active.generation_id if self._active else None

    @property
    def staging_generation_id(self) -> str | None:
        with self._lock:
            return self._staging.generation_id if self._staging else None

    def _remember_request(self, request_id: str) -> None:
        if request_id in self._recent_id_set or request_id in self._outstanding:
            raise BrokerProtocolError("duplicate request ID")
        self._recent_ids.append(request_id)
        self._recent_id_set.add(request_id)
        while len(self._recent_ids) > 256:
            expired = self._recent_ids.popleft()
            self._recent_id_set.discard(expired)

    @staticmethod
    def _bounded_text(value: object, maximum: int = 1024) -> str:
        text = str(value).replace("\n", " ").replace("\r", " ")
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= maximum:
            return text
        return encoded[:maximum].decode("utf-8", errors="ignore")

    def _emit(self, type_: str, request_id: str, **values: Any) -> None:
        message = {"type": type_, "requestId": request_id, **values}
        self.output.write(message)
        self.high_water.output_messages = max(self.high_water.output_messages, 1)
        self.high_water.output_bytes = max(
            self.high_water.output_bytes, self.output.max_message_bytes
        )

    def _error(self, request_id: str, code: str, detail: object) -> None:
        self._emit(
            "error", request_id, code=code,
            error=self._bounded_text(detail),
        )

    def handle(self, request: BrokerRequest) -> None:
        try:
            with self._lock:
                if self._shutdown and request.operation != "shutdown":
                    raise BrokerProtocolError("broker is shutting down")
                self._remember_request(request.request_id)
            if request.operation == "hello":
                self._emit("ready", request.request_id, capabilities={
                    "operations": sorted((
                        "scanStart", "scanCancel", "metadata", "children",
                        "snapshotOpen",
                        "ancestors", "search", "childrenAt", "activationCommit",
                        "activationAbort", "shutdown",
                    )),
                    "maxRows": self.query_limits.max_rows,
                    "maxOutstandingRequests": MAX_OUTSTANDING_REQUESTS,
                })
            elif request.operation == "scanStart":
                self._scan_start(request)
            elif request.operation == "snapshotOpen":
                self._snapshot_open(request)
            elif request.operation == "scanCancel":
                self._scan_cancel(request)
            elif request.operation in QUERY_OPERATIONS:
                self._enqueue_query(request)
            elif request.operation == "activationCommit":
                self._activation_commit(request)
            elif request.operation == "activationAbort":
                self._activation_abort(request)
            elif request.operation == "shutdown":
                self.shutdown(request.request_id)
        except BrokerProtocolError as error:
            self._error(request.request_id, "invalidRequest", error)
        except ResourceLimitExceeded as error:
            self._error(request.request_id, "resourceLimit", error)
        except BaseException:
            self._error(request.request_id, "internalError", "internal broker failure")

    def _string(self, request: BrokerRequest, key: str, maximum: int) -> str:
        value = request.values.get(key)
        if not isinstance(value, str) or not value:
            raise BrokerProtocolError(f"{key} must be a nonempty string")
        try:
            size = len(value.encode("utf-8", errors="surrogateescape"))
        except UnicodeEncodeError as error:
            raise BrokerProtocolError(f"{key} is not safely encodable") from error
        if size > maximum:
            raise BrokerProtocolError(f"{key} exceeds byte limit")
        return value

    def _generation_id(self, request: BrokerRequest, required: bool = False) -> str:
        value = request.values.get("generationId")
        if value is None and not required:
            raise BrokerProtocolError("generation ID is required")
        return validate_identifier(value, "generation ID", MAX_GENERATION_ID_BYTES)

    def _scan_start(self, request: BrokerRequest) -> None:
        path = canonical_path(self._string(
            request, "path", self.resource_limits.max_path_bytes
        ))
        mountpoint = canonical_path(self._string(
            request, "mountpoint", self.resource_limits.max_path_bytes
        ))
        check_path(path, self.resource_limits)
        check_path(mountpoint, self.resource_limits)
        if "generationId" in request.values:
            raise BrokerProtocolError("scan generation IDs are broker-generated")
        generation_id = secrets.token_hex(16)
        cancellation = Cancellation()
        state = _ScanState(
            generation_id, request.request_id, secrets.token_hex(16), cancellation
        )
        with self._lock:
            if self._staging is not None:
                raise BrokerProtocolError("a scan is already running")
            if self._previous is not None:
                raise BrokerProtocolError("snapshot activation is not settled")
            if any(
                self._active is None or job.generation is not self._active
                for job in self._outstanding.values()
            ):
                raise BrokerProtocolError("a retired snapshot query is still active")
            if self._active and self._active.generation_id == generation_id:
                raise BrokerProtocolError("generation ID is already active")
            self._staging = state
            self.high_water.scan_workers = max(self.high_water.scan_workers, 1)
            self.high_water.staging_generations = max(
                self.high_water.staging_generations, 1
            )
        thread = threading.Thread(
            target=self._scan_worker,
            args=(state, path, mountpoint),
            name="omatree-scan", daemon=True,
        )
        state.thread = thread
        timer = threading.Timer(self.scan_timeout, self._scan_timed_out, args=(state,))
        timer.daemon = True
        state.timer = timer
        self._emit(
            "scanStarted", request.request_id,
            generationId=generation_id, path=path, mountpoint=mountpoint,
        )
        timer.start()
        thread.start()

    def _snapshot_open(self, request: BrokerRequest) -> None:
        path = canonical_path(self._string(request, "path", self.resource_limits.max_path_bytes))
        mountpoint = canonical_path(self._string(request, "mountpoint", self.resource_limits.max_path_bytes))
        check_path(path, self.resource_limits)
        check_path(mountpoint, self.resource_limits)
        with self._lock:
            if self._staging is not None or self._previous is not None:
                raise BrokerProtocolError("snapshot activation is not settled")
        snapshot = self._snapshot_store().load(path, mountpoint)
        if snapshot is None:
            self._emit("snapshotMissing", request.request_id, path=path, mountpoint=mountpoint)
            return
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            summary = {
                "bytes": reader._metadata_value("allocated_bytes"),
                "directFilesBytes": reader._metadata_value("direct_files_bytes"),
                "entries": reader._metadata_value("entries"),
                "directoryCount": reader.directory_count,
                "fileCount": reader.file_count,
                "warningCount": reader._metadata_value("warning_count"),
                "durationMs": reader._metadata_value("duration_ms"),
                "createdAtMs": reader._metadata_value("created_at_ms"),
            }
        generation_id = secrets.token_hex(16)
        with self._lock:
            self._previous = self._active
            self._active = _Generation(generation_id, snapshot, mountpoint=mountpoint,
                                       persistent=True)
        self._emit("snapshotAvailable", request.request_id, generationId=generation_id,
                   path=path, **summary)

    def _scan_timed_out(self, state: _ScanState) -> None:
        with self._lock:
            if self._staging is state and not state.terminal_emitted:
                state.timeout = True
                state.cancel_requested = True
                state.cancellation.cancel()

    def _scan_cancel(self, request: BrokerRequest) -> None:
        generation_id = self._generation_id(request, required=True)
        with self._lock:
            state = self._staging
            if state is None or state.generation_id != generation_id:
                raise BrokerProtocolError("generation is not staging")
            if not state.cancel_requested:
                state.cancel_requested = True
                state.cancellation.cancel()
        self._emit(
            "scanCancelRequested", request.request_id,
            generationId=generation_id,
        )

    def _scan_event(self, state: _ScanState, event: dict[str, Any]) -> None:
        with self._lock:
            if self._staging is not state or state.cancel_requested:
                return
        if event.get("type") == scan_protocol.PROGRESS:
            now = time.monotonic()
            with self._lock:
                if now - state.last_progress_emit < PROGRESS_INTERVAL_SECONDS:
                    return
                state.last_progress_emit = now
            self._emit(
                "scanProgress", state.request_id,
                generationId=state.generation_id,
                entries=event.get("entries", 0), bytes=event.get("bytes", 0),
            )
        elif event.get("type") == scan_protocol.WARNING:
            with self._lock:
                if state.warnings_emitted >= MAX_WARNINGS:
                    return
                state.warnings_emitted += 1
            path = event.get("path") if isinstance(event.get("path"), str) else ""
            if len(path.encode("utf-8", errors="replace")) > self.resource_limits.max_path_bytes:
                path = ""
            self._emit(
                "scanWarning", state.request_id,
                generationId=state.generation_id, path=path,
                error=self._bounded_text(event.get("error", "scan warning")),
            )

    def _scan_worker(
        self, state: _ScanState, path: str, mountpoint: str,
    ) -> None:
        started = time.monotonic()
        writer: SQLiteSnapshotWriter | None = None
        snapshot: SQLiteSnapshot | None = None
        reporter = ScanReporter(
            state.request_id,
            lambda event: self._scan_event(state, event),
            progress_interval=PROGRESS_INTERVAL_SECONDS,
            limits=self.resource_limits,
        )
        try:
            validation_error = validate_scan_root(path, mountpoint)
            if validation_error is not None:
                raise BrokerProtocolError(validation_error)
            exclusions = self.exclusion_provider(mountpoint)
            if is_excluded(path, exclusions):
                raise BrokerProtocolError(
                    "scan path belongs to a descendant mounted filesystem"
                )
            state.cancellation.check()
            writer = SQLiteSnapshotWriter.create_in_directory(
                self._snapshot_store().root,
                resource_limits=self.resource_limits,
                sqlite_limits=self.sqlite_limits,
            )
            state.writer = writer
            scan_arguments = (
                path, exclusions, reporter, state.cancellation,
                writer.insert_directory, self.resource_limits,
            )
            if "emit_file" in inspect.signature(self.scan_function).parameters:
                summary = self.scan_function(*scan_arguments, emit_file=writer.insert_file)
            else:
                summary = self.scan_function(*scan_arguments)
            state.cancellation.check()
            snapshot = writer.finalize(
                path, summary["directoryCount"], {
                    "entries": reporter.entries,
                    "allocated_bytes": summary["bytes"],
                    "direct_files_bytes": summary["directFilesBytes"],
                    "warning_count": reporter.warning_count,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "created_at_ms": round(time.time() * 1000),
                    "mountpoint": mountpoint,
                },
            )
            state.snapshot = snapshot
            state.cancellation.check()
            self._activate(state, snapshot, summary, reporter, started, mountpoint)
            state.snapshot = None
            snapshot = None
        except ScanCancelled:
            self._finish_scan(state, "scanFailed" if state.timeout else "scanCancelled",
                              "scan timed out" if state.timeout else None)
        except (
            BrokerProtocolError, ResourceLimitExceeded, SQLiteQueryError,
            SQLiteSnapshotError, OSError, sqlite3.Error,
        ) as error:
            self._finish_scan(state, "scanFailed", error)
        except BaseException:
            self._finish_scan(state, "scanFailed", "internal scan failure")
        finally:
            if state.timer is not None:
                state.timer.cancel()
            if snapshot is not None:
                snapshot.delete()
            if writer is not None and not writer._completed:
                writer.abort()
            state.writer = None

    def _activate(
        self,
        state: _ScanState,
        snapshot: SQLiteSnapshot,
        summary: Mapping[str, Any],
        reporter: ScanReporter,
        started: float,
        mountpoint: str,
    ) -> None:
        with self._lock:
            if (
                self._staging is not state or state.cancel_requested
                or state.cancellation.cancelled or state.terminal_emitted
            ):
                raise ScanCancelled
            # Opening here proves the committed database satisfies the read contract.
            with SQLiteSnapshotReader.open(
                snapshot.path,
                query_limits=self.query_limits,
                resource_limits=self.resource_limits,
                sqlite_limits=self.sqlite_limits,
            ):
                pass
            self._previous = self._active
            self._active = _Generation(state.generation_id, snapshot, mountpoint=mountpoint)
            self._staging = None
            state.terminal_emitted = True
            self._condition.notify_all()
            self.high_water.active_generations = max(
                self.high_water.active_generations, 1
            )
        self._emit(
            "snapshotActivated", state.request_id,
            generationId=state.generation_id,
            path=snapshot.root_path,
            bytes=summary["bytes"],
            directFilesBytes=summary["directFilesBytes"],
            entries=reporter.entries,
            directoryCount=summary["directoryCount"],
            fileCount=summary.get("fileCount", snapshot.file_count),
            warningCount=reporter.warning_count,
            suppressedWarningCount=max(0, reporter.warning_count - MAX_WARNINGS),
            durationMs=round((time.monotonic() - started) * 1000),
            createdAtMs=round(time.time() * 1000),
        )

    def _activation_commit(self, request: BrokerRequest) -> None:
        generation_id = self._generation_id(request, required=True)
        with self._lock:
            if self._active is None or self._active.generation_id != generation_id:
                raise BrokerProtocolError("generation is not awaiting activation")
            previous = self._previous
            current = self._active
        if current is not None and not current.persistent:
            try:
                current.snapshot = self._snapshot_store().publish(
                    current.snapshot, current.mountpoint
                )
                current.persistent = True
            except (OSError, SQLiteSnapshotError) as error:
                raise BrokerProtocolError("could not persist completed snapshot") from error
        with self._lock:
            self._previous = None
            if previous is not None:
                previous.retired = True
        if previous is not None and previous.references == 0:
            previous.snapshot.delete()
        self._emit(
            "activationCommitted", request.request_id,
            generationId=generation_id,
        )

    def _snapshot_store(self) -> PersistentSnapshotStore:
        if self.snapshot_store is None:
            self.snapshot_store = PersistentSnapshotStore(self._cache_dir)
        return self.snapshot_store

    def _activation_abort(self, request: BrokerRequest) -> None:
        generation_id = self._generation_id(request, required=True)
        with self._lock:
            current = self._active
            if current is None or current.generation_id != generation_id:
                raise BrokerProtocolError("generation is not awaiting activation")
            current.retired = True
            self._active = self._previous
            self._previous = None
            restored = self._active.generation_id if self._active else ""
        if current.references == 0:
            if not current.persistent:
                current.snapshot.delete()
        self._emit(
            "activationAborted", request.request_id,
            generationId=generation_id, activeGenerationId=restored,
        )

    def _finish_scan(
        self, state: _ScanState, outcome: str, error: object | None,
    ) -> None:
        if state.snapshot is not None:
            state.snapshot.delete()
            state.snapshot = None
        if state.writer is not None and not state.writer._completed:
            state.writer.abort()
            state.writer = None
        with self._lock:
            if self._staging is not state or state.terminal_emitted:
                return
            self._staging = None
            state.terminal_emitted = True
            shutting_down = self._shutdown
            self._condition.notify_all()
        if shutting_down:
            return
        values: dict[str, Any] = {"generationId": state.generation_id}
        if error is not None:
            values["error"] = self._bounded_text(error)
        self._emit(outcome, state.request_id, **values)

    def _enqueue_query(self, request: BrokerRequest) -> None:
        generation_id = self._generation_id(request, required=True)
        superseded_jobs: list[_QueryJob] = []
        with self._condition:
            generation = self._active
            if generation is None or generation.generation_id != generation_id:
                generation = self._previous
            if generation is None or generation.generation_id != generation_id:
                raise BrokerProtocolError("generation is not active")
            if request.operation == "search":
                retained: deque[_QueryJob] = deque()
                while self._query_jobs:
                    job = self._query_jobs.popleft()
                    if job.request.operation == "search":
                        job.superseded.set()
                        self._outstanding.pop(job.request.request_id, None)
                        job.generation.references -= 1
                        superseded_jobs.append(job)
                    else:
                        retained.append(job)
                self._query_jobs = retained
                if self._current_query and self._current_query.request.operation == "search":
                    self._current_query.superseded.set()
                    if self._current_reader is not None:
                        self._current_reader.interrupt()
            if len(self._outstanding) >= MAX_OUTSTANDING_REQUESTS:
                raise BrokerProtocolError("too many outstanding requests")
            job = _QueryJob(request, generation, threading.Event())
            generation.references += 1
            self._outstanding[request.request_id] = job
            self._query_jobs.append(job)
            self.high_water.outstanding_requests = max(
                self.high_water.outstanding_requests, len(self._outstanding)
            )
            self._condition.notify()
        for old in superseded_jobs:
            self._error(old.request.request_id, "superseded", "search was superseded")

    def _query_loop(self) -> None:
        while True:
            with self._condition:
                while not self._query_jobs and not self._shutdown:
                    self._condition.wait()
                if self._shutdown and not self._query_jobs:
                    return
                job = self._query_jobs.popleft()
                self._current_query = job
                if job.request.operation == "search":
                    self.high_water.search_workers = max(
                        self.high_water.search_workers, 1
                    )
            self._run_query(job)

    def _run_query(self, job: _QueryJob) -> None:
        reader: SQLiteSnapshotReader | None = None
        try:
            if job.superseded.is_set():
                self._error(job.request.request_id, "superseded", "search was superseded")
                return
            reader = SQLiteSnapshotReader.open(
                job.generation.snapshot.path,
                query_limits=self.query_limits,
                resource_limits=self.resource_limits,
                sqlite_limits=self.sqlite_limits,
            )
            with self._lock:
                self._current_reader = reader
            timeout = (
                self.search_timeout
                if job.request.operation == "search" else self.query_timeout
            )
            deadline = time.monotonic() + timeout
            reader._connection.set_progress_handler(
                lambda: int(job.superseded.is_set() or time.monotonic() >= deadline),
                1000,
            )
            result = self._execute_query(reader, job.request)
            reader._connection.set_progress_handler(None, 0)
            if job.superseded.is_set():
                self._error(job.request.request_id, "superseded", "search was superseded")
                return
            if time.monotonic() >= deadline:
                self._error(job.request.request_id, "queryTimeout", "query timed out")
                return
            page_data = json.loads(encode_query_page(result, self.query_limits))
            self.output.write({
                "type": "queryResult",
                "requestId": job.request.request_id,
                "generationId": job.generation.generation_id,
                "operation": job.request.operation,
                "result": page_data,
            }, query=True)
            self.high_water.output_bytes = max(
                self.high_water.output_bytes, self.output.max_message_bytes
            )
        except (SQLiteQueryError, sqlite3.Error, BrokerProtocolError) as error:
            if job.superseded.is_set():
                self._error(job.request.request_id, "superseded", "search was superseded")
            elif time.monotonic() >= locals().get("deadline", float("inf")):
                self._error(job.request.request_id, "queryTimeout", "query timed out")
            else:
                self._error(job.request.request_id, "queryFailed", error)
        except BaseException:
            self._error(job.request.request_id, "internalError", "internal query failure")
        finally:
            if reader is not None:
                try:
                    reader._connection.set_progress_handler(None, 0)
                except sqlite3.Error:
                    pass
                reader.close()
            with self._condition:
                self._current_reader = None
                self._current_query = None
                job.generation.references -= 1
                if job.generation.retired and job.generation.references == 0:
                    if not job.generation.persistent:
                        job.generation.snapshot.delete()
                self._outstanding.pop(job.request.request_id, None)
                self._condition.notify_all()

    def _execute_query(self, reader: SQLiteSnapshotReader, request: BrokerRequest):
        continuation = request.values.get("continuation")
        if continuation is not None:
            try:
                continuation_size = len(
                    continuation.encode("utf-8", errors="surrogateescape")
                ) if isinstance(continuation, str) else MAX_CONTINUATION_BYTES + 1
            except UnicodeEncodeError as error:
                raise BrokerProtocolError(
                    "continuation is not safely encodable"
                ) from error
            if continuation_size > MAX_CONTINUATION_BYTES:
                raise BrokerProtocolError("continuation exceeds byte limit")
        if request.operation == "metadata":
            path = self._string(request, "path", self.resource_limits.max_path_bytes)
            row = reader.metadata(path)
            from .sqlite_query import QueryPage
            return QueryPage("metadata", () if row is None else (row,), False, None)
        if request.operation == "children":
            path = self._string(request, "path", self.resource_limits.max_path_bytes)
            return reader.children(path, continuation)
        if request.operation == "childrenAt":
            path = self._string(request, "path", self.resource_limits.max_path_bytes)
            return reader.children_at(path)
        if request.operation == "ancestors":
            path = self._string(request, "path", self.resource_limits.max_path_bytes)
            return reader.ancestors(path, continuation)
        query = self._string(
            request, "query", self.query_limits.max_search_query_bytes
        )
        return reader.search(query, continuation)

    def shutdown(self, request_id: str = "shutdown") -> None:
        with self._condition:
            if self._shutdown:
                return
            self._shutdown = True
            state = self._staging
            if state is not None:
                state.cancel_requested = True
                state.cancellation.cancel()
            pending = list(self._query_jobs)
            self._query_jobs.clear()
            for job in pending:
                job.superseded.set()
                self._outstanding.pop(job.request.request_id, None)
                job.generation.references -= 1
            if self._current_query is not None:
                self._current_query.superseded.set()
                if self._current_reader is not None:
                    self._current_reader.interrupt()
            self._condition.notify_all()
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
        scan_thread = state.thread if state is not None else None
        if scan_thread is not None and scan_thread is not threading.current_thread():
            scan_thread.join(max(0, deadline - time.monotonic()))
        if self._query_thread is not threading.current_thread():
            self._query_thread.join(max(0, deadline - time.monotonic()))
        with self._lock:
            active = self._active
            previous = self._previous
            self._active = None
            self._previous = None
            if active is not None:
                active.retired = True
            if previous is not None:
                previous.retired = True
        if active is not None and active.references == 0:
            if not active.persistent:
                active.snapshot.delete()
        if previous is not None and previous.references == 0:
            if not previous.persistent:
                previous.snapshot.delete()
        try:
            self.session_dir.rmdir()
        except OSError:
            pass
        try:
            self._emit("shutdownComplete", request_id)
        except (BrokenPipeError, BrokerProtocolError):
            pass

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._staging is not None or self._outstanding:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def wait_for_queries(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._outstanding:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


def serve(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    runtime_dir: str | os.PathLike[str] | None = None,
) -> int:
    output = BoundedOutputWriter(output_stream)
    broker = SnapshotBroker(output, runtime_dir=runtime_dir)
    output.write({
        "type": "ready", "requestId": "startup",
        "capabilities": {"protocolVersion": BROKER_PROTOCOL_VERSION},
    })
    try:
        while True:
            raw = input_stream.readline(MAX_REQUEST_LINE_BYTES + 1)
            if not raw:
                broker.shutdown("eof")
                return 0
            if len(raw) > MAX_INPUT_BUFFER_BYTES:
                raise BrokerStreamError("input buffer exceeded")
            if len(raw) > MAX_REQUEST_LINE_BYTES:
                if not raw.endswith(b"\n"):
                    raise BrokerStreamError("oversized unterminated request frame")
                output.write({
                    "type": "error", "requestId": "unknown",
                    "code": "malformedRequest",
                    "error": "request line exceeds 65536 bytes",
                })
                continue
            try:
                request = parse_request_line(raw)
            except BrokerStreamError:
                raise
            except BrokerProtocolError as error:
                output.write({
                    "type": "error", "requestId": "unknown",
                    "code": "malformedRequest", "error": str(error),
                })
                continue
            broker.handle(request)
            if request.operation == "shutdown":
                return 0
    except BrokerStreamError:
        broker.shutdown("stream-error")
        return 2
    except (BrokenPipeError, OSError):
        broker.shutdown("output-failure")
        return 1
    except Exception:
        broker.shutdown("internal-error")
        return 1


def install_parent_death_signal() -> bool:
    """Best-effort Linux PDEATHSIG without a third-party dependency."""
    if os.name != "posix" or not Path("/proc/self/status").exists():
        return False
    try:
        import ctypes
        parent = os.getppid()
        if parent == 1:
            raise SystemExit("OmaTree broker parent is unavailable")
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            return False
        if os.getppid() != parent:
            raise SystemExit("OmaTree broker parent exited during startup")
        return True
    except (AttributeError, OSError):
        return False
