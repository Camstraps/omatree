import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import sys

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "helper"))

from omatree_core import protocol as scan_protocol
from omatree_core.broker import (
    BoundedOutputWriter,
    SnapshotBroker,
    create_broker_session,
    serve,
)
from omatree_core.broker_protocol import (
    BROKER_PROTOCOL_VERSION,
    BrokerProtocolError,
    BrokerRequest,
    BrokerStreamError,
    MAX_REQUEST_LINE_BYTES,
    encode_message,
    parse_request_line,
)
from omatree_core.scanner import ScanCancelled
from omatree_core.sqlite_query import SQLiteSnapshotReader
from omatree_core.sqlite_snapshot import SQLiteSnapshotWriter


def request(request_id, operation, **values):
    return BrokerRequest(request_id, operation, {
        "protocolVersion": BROKER_PROTOCOL_VERSION,
        "requestId": request_id,
        "operation": operation,
        **values,
    })


class FakeScanner:
    def __init__(self):
        self.behavior = {}
        self.started = threading.Event()
        self.near_complete = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.active = 0
        self.peak_active = 0

    def __call__(
        self, path, exclusions, reporter, cancellation, emit_directory, limits,
    ):
        self.calls += 1
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        mode = self.behavior.get(path, "success")
        self.started.set()
        try:
            if mode == "fail":
                raise OSError("synthetic failure")
            if mode in ("slow", "near"):
                if mode == "near":
                    self.near_complete.set()
                while not self.release.wait(0.005):
                    if mode == "slow":
                        cancellation.check()
                cancellation.check()
            reporter.account(0)
            child = path + "/child"
            emit_directory({
                "path": child, "parentPath": path, "name": "child",
                "bytes": 20, "directFilesBytes": 10,
                "childDirectoryCount": 0, "warningCount": 0,
            })
            emit_directory({
                "path": path, "parentPath": None, "name": Path(path).name,
                "bytes": 30, "directFilesBytes": 10,
                "childDirectoryCount": 1, "warningCount": 0,
            })
            return {"bytes": 30, "directFilesBytes": 10, "directoryCount": 2}
        finally:
            self.active -= 1


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name)
        self.runtime.chmod(0o700)
        self.root_a = self.runtime / "a"
        self.root_b = self.runtime / "b"
        self.root_c = self.runtime / "c"
        for path in (self.root_a, self.root_b, self.root_c):
            path.mkdir()
        self.bytes = io.BytesIO()
        self.output = BoundedOutputWriter(self.bytes)
        self.scanner = FakeScanner()
        self.broker = SnapshotBroker(
            self.output,
            runtime_dir=self.runtime,
            scan_function=self.scanner,
            exclusion_provider=lambda _mount: set(),
            scan_timeout=2,
            query_timeout=0.2,
            search_timeout=0.2,
        )
        self.counter = 0

    def tearDown(self):
        self.scanner.release.set()
        self.broker.shutdown("teardown")
        self.temporary.cleanup()

    def messages(self):
        return [json.loads(line) for line in self.bytes.getvalue().splitlines()]

    def next_id(self, prefix="r"):
        self.counter += 1
        return f"{prefix}{self.counter}"

    def start(self, root, request_id=None):
        self.scanner.started.clear()
        scan_request_id = request_id or self.next_id("scan")
        self.broker.handle(request(
            scan_request_id, "scanStart", path=str(root), mountpoint=str(root),
        ))
        return next(
            item["generationId"] for item in reversed(self.messages())
            if item.get("requestId") == scan_request_id
            and item.get("type") == "scanStarted"
        )

    def wait_active(self, generation, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.broker.active_generation_id == generation:
                return
            time.sleep(0.005)
        self.fail(f"generation {generation} did not activate")

    def activate(self, root=None):
        generation = self.start(root or self.root_a)
        self.wait_active(generation)
        return generation

    def commit_activation(self, generation):
        request_id = self.next_id("commit")
        self.broker.handle(request(
            request_id, "activationCommit", generationId=generation
        ))
        return request_id

    def query(self, operation, generation=None, **values):
        generation = generation or self.broker.active_generation_id
        request_id = self.next_id("query")
        self.broker.handle(request(
            request_id, operation, generationId=generation, **values
        ))
        self.assertTrue(self.broker.wait_for_queries())
        return next(
            item for item in reversed(self.messages())
            if item.get("requestId") == request_id
        )

    def test_hello_and_capabilities(self):
        self.broker.handle(request("hello1", "hello"))
        message = self.messages()[-1]
        self.assertEqual(message["type"], "ready")
        self.assertEqual(message["capabilities"]["maxRows"], 64)
        self.assertEqual(message["capabilities"]["maxOutstandingRequests"], 4)
        self.assertIn("childrenAt", message["capabilities"]["operations"])

    def test_valid_scan_activates_and_is_queryable(self):
        generation = self.activate()
        activated = [m for m in self.messages() if m["type"] == "snapshotActivated"]
        self.assertEqual(len(activated), 1)
        result = self.query("children", path=str(self.root_a))
        self.assertEqual(result["type"], "queryResult")
        self.assertEqual(result["generationId"], generation)
        self.assertEqual(len(result["result"]["rows"]), 1)
        self.assertLessEqual(len(json.dumps(result).encode()), 1024 * 1024)
        revealed = self.query("childrenAt", path=str(self.root_a / "child"))
        self.assertEqual(revealed["type"], "queryResult")
        self.assertEqual(revealed["result"]["kind"], "childrenAt")
        self.assertEqual(revealed["result"]["rows"][0]["path"], str(self.root_a / "child"))

    def test_a_remains_queryable_while_b_scans_and_b_is_not_queryable(self):
        active = self.activate()
        self.scanner.behavior[str(self.root_b)] = "slow"
        staging = self.start(self.root_b)
        self.assertTrue(self.scanner.started.wait(1))
        self.assertEqual(
            self.query("metadata", path=str(self.root_a))["type"], "queryResult"
        )
        response = self.query("metadata", generation=staging, path=str(self.root_b))
        self.assertEqual(response["type"], "error")
        self.scanner.release.set()
        self.wait_active(staging)

    def test_successful_replacement_deletes_retired_database(self):
        self.activate()
        old_path = next(self.broker.session_dir.glob("*.sqlite"))
        staging = self.start(self.root_b)
        self.wait_active(staging)
        self.commit_activation(staging)
        self.assertFalse(old_path.exists())
        self.assertEqual(len(list(self.broker.session_dir.glob("*.sqlite"))), 1)

    def test_activation_abort_restores_previous_generation(self):
        original = self.activate()
        original_path = next(self.broker.session_dir.glob("*.sqlite"))
        replacement = self.start(self.root_b)
        self.wait_active(replacement)
        self.broker.handle(request(
            "abort-replacement", "activationAbort", generationId=replacement
        ))
        self.assertEqual(self.broker.active_generation_id, original)
        self.assertTrue(original_path.exists())
        self.assertEqual(len(list(self.broker.session_dir.glob("*.sqlite"))), 1)

    def test_retired_database_waits_for_inflight_query(self):
        self.activate()
        old_path = next(self.broker.session_dir.glob("*.sqlite"))
        entered = threading.Event()
        release = threading.Event()
        original = SQLiteSnapshotReader.metadata

        def delayed(reader, path):
            entered.set()
            release.wait(1)
            return original(reader, path)

        with mock.patch.object(SQLiteSnapshotReader, "metadata", delayed):
            self.broker.handle(request(
                "old-query", "metadata",
                generationId=self.broker.active_generation_id,
                path=str(self.root_a),
            ))
            self.assertTrue(entered.wait(1))
            replacement = self.start(self.root_b)
            self.wait_active(replacement)
            self.assertTrue(old_path.exists())
            self.commit_activation(replacement)
            release.set()
            self.assertTrue(self.broker.wait_for_queries())
        self.assertFalse(old_path.exists())

    def test_failed_b_preserves_a_and_removes_staging(self):
        active = self.activate()
        active_path = next(self.broker.session_dir.glob("*.sqlite"))
        self.scanner.behavior[str(self.root_b)] = "fail"
        self.start(self.root_b)
        self.assertTrue(self.broker.wait_for_idle())
        self.assertEqual(self.broker.active_generation_id, active)
        self.assertTrue(active_path.exists())
        self.assertEqual(len(list(self.broker.session_dir.glob("*.sqlite"))), 1)
        self.assertEqual(self.query("metadata", path=str(self.root_a))["type"], "queryResult")

    def test_cancelled_b_preserves_a_and_duplicate_cancel_is_idempotent(self):
        active = self.activate()
        self.scanner.release.clear()
        self.scanner.behavior[str(self.root_b)] = "near"
        staging = self.start(self.root_b)
        self.assertTrue(self.scanner.near_complete.wait(1))
        self.broker.handle(request("cancel1", "scanCancel", generationId=staging))
        self.broker.handle(request("cancel2", "scanCancel", generationId=staging))
        self.scanner.release.set()
        self.assertTrue(self.broker.wait_for_idle())
        self.assertEqual(self.broker.active_generation_id, active)
        self.assertFalse(any(
            m["type"] == "snapshotActivated" and m.get("generationId") == staging
            for m in self.messages()
        ))
        terminals = [
            m for m in self.messages()
            if m["type"] in ("scanCancelled", "scanFailed")
            and m.get("generationId") == staging
        ]
        self.assertEqual(len(terminals), 1)

    def test_cancel_immediately_and_new_scan_after_cancellation(self):
        self.scanner.behavior[str(self.root_a)] = "slow"
        staging = self.start(self.root_a)
        self.broker.handle(request("cancel-now", "scanCancel", generationId=staging))
        self.assertTrue(self.broker.wait_for_idle())
        self.assertIsNone(self.broker.active_generation_id)
        self.scanner.behavior[str(self.root_b)] = "success"
        next_generation = self.start(self.root_b)
        self.wait_active(next_generation)
        self.assertEqual(self.scanner.peak_active, 1)

    def test_cancel_during_sqlite_validation_cannot_activate(self):
        entered = threading.Event()
        release = threading.Event()
        original = SQLiteSnapshotWriter._validate

        def delayed(writer, root_path, expected_count):
            entered.set()
            release.wait(1)
            return original(writer, root_path, expected_count)

        with mock.patch.object(SQLiteSnapshotWriter, "_validate", delayed):
            generation = self.start(self.root_a)
            self.assertTrue(entered.wait(1))
            self.broker.handle(request(
                "cancel-validation", "scanCancel", generationId=generation
            ))
            release.set()
            self.assertTrue(self.broker.wait_for_idle())
        self.assertIsNone(self.broker.active_generation_id)
        self.assertEqual(list(self.broker.session_dir.glob("*.sqlite")), [])
        self.assertFalse(any(
            message["type"] == "snapshotActivated"
            for message in self.messages()
        ))

    def test_scan_timeout_never_activates(self):
        self.broker.scan_timeout = 0.03
        self.scanner.behavior[str(self.root_a)] = "slow"
        self.start(self.root_a)
        self.assertTrue(self.broker.wait_for_idle())
        self.assertIsNone(self.broker.active_generation_id)
        self.assertTrue(any(
            m["type"] == "scanFailed" and "timed out" in m.get("error", "")
            for m in self.messages()
        ))

    def test_stale_generation_and_continuation_are_rejected(self):
        self.activate()
        stale = self.query("metadata", generation="old", path=str(self.root_a))
        self.assertEqual(stale["type"], "error")
        bad = self.query(
            "children", path=str(self.root_a), continuation="not-a-token"
        )
        self.assertEqual(bad["type"], "error")

    def test_query_operations_do_not_traverse_scanned_filesystem(self):
        self.activate()
        with mock.patch("omatree_core.scanner.os.scandir", side_effect=AssertionError):
            for operation, values in (
                ("metadata", {"path": str(self.root_a)}),
                ("children", {"path": str(self.root_a)}),
                ("ancestors", {"path": str(self.root_a / "child")}),
                ("search", {"query": "child"}),
            ):
                self.assertEqual(self.query(operation, **values)["type"], "queryResult")

    def test_malformed_active_database_fails_safely_without_traceback(self):
        self.activate()
        database = next(self.broker.session_dir.glob("*.sqlite"))
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE directories SET allocated_bytes = 'bad' WHERE parent_path IS NOT NULL"
        )
        connection.commit()
        connection.close()
        response = self.query("children", path=str(self.root_a))
        self.assertEqual(response["type"], "error")
        self.assertNotIn("Traceback", self.bytes.getvalue().decode())

    def test_more_than_four_outstanding_queries_are_rejected(self):
        self.activate()
        self.output._lock.acquire()
        try:
            for index in range(4):
                req = request(
                    f"held{index}", "metadata", generationId=self.broker.active_generation_id,
                    path=str(self.root_a),
                )
                with self.broker._lock:
                    self.broker._remember_request(req.request_id)
                self.broker._enqueue_query(req)
            fifth = request(
                "held4", "metadata", generationId=self.broker.active_generation_id, path=str(self.root_a)
            )
            with self.broker._lock:
                self.broker._remember_request(fifth.request_id)
            with self.assertRaisesRegex(BrokerProtocolError, "too many"):
                self.broker._enqueue_query(fifth)
            self.assertEqual(self.broker.high_water.outstanding_requests, 4)
        finally:
            self.output._lock.release()
        self.assertTrue(self.broker.wait_for_idle())

    def test_search_supersession_keeps_one_search_worker(self):
        self.activate()
        original = SQLiteSnapshotReader.search
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def delayed(reader, query, continuation=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(1)
            return original(reader, query, continuation)

        with mock.patch.object(SQLiteSnapshotReader, "search", delayed):
            self.broker.handle(request(
                "search-old", "search", generationId=self.broker.active_generation_id, query="c"
            ))
            self.assertTrue(entered.wait(1))
            self.broker.handle(request(
                "search-new", "search", generationId=self.broker.active_generation_id, query="ch"
            ))
            release.set()
            self.assertTrue(self.broker.wait_for_idle())
        old = [m for m in self.messages() if m.get("requestId") == "search-old"][-1]
        new = [m for m in self.messages() if m.get("requestId") == "search-new"][-1]
        self.assertEqual(old.get("code"), "superseded")
        self.assertEqual(new["type"], "queryResult")
        self.assertEqual(self.broker.high_water.search_workers, 1)

    def test_query_timeout_is_controlled(self):
        active = self.activate()
        original = SQLiteSnapshotReader.metadata

        def delayed(reader, path):
            time.sleep(0.03)
            return original(reader, path)

        self.broker.query_timeout = 0.01
        with mock.patch.object(SQLiteSnapshotReader, "metadata", delayed):
            result = self.query("metadata", path=str(self.root_a))
        self.assertEqual(result.get("code"), "queryTimeout")
        self.assertEqual(self.broker.active_generation_id, active)

    def test_progress_and_warning_forwarding_are_bounded(self):
        state = mock.Mock()
        state.request_id = "scan"
        state.generation_id = "A"
        state.cancel_requested = False
        state.last_progress_emit = 0.0
        state.warnings_emitted = 0
        with self.broker._lock:
            self.broker._staging = state
        for index in range(1_000):
            self.broker._scan_event(state, scan_protocol.progress_event("scan", index, 0))
            self.broker._scan_event(state, scan_protocol.warning_event("scan", "/x", "w"))
        progress = [m for m in self.messages() if m["type"] == "scanProgress"]
        warnings = [m for m in self.messages() if m["type"] == "scanWarning"]
        self.assertLessEqual(len(progress), 1)
        self.assertEqual(len(warnings), 100)
        with self.broker._lock:
            self.broker._staging = None

    def test_output_writer_has_no_queue_and_enforces_message_bounds(self):
        self.assertFalse(any(
            isinstance(value, (list, dict, set))
            for value in self.output.__dict__.values()
        ))
        self.output.write({"type": "ok", "requestId": "x"})
        self.assertEqual(self.output.messages_written, 1)
        with self.assertRaises(BrokerProtocolError):
            self.output.write({"type": "x", "requestId": "x", "data": "z" * 65536})

    def test_repeated_generation_cycle_leaves_one_database(self):
        self.activate()
        self.scanner.behavior[str(self.root_b)] = "fail"
        self.start(self.root_b)
        self.assertTrue(self.broker.wait_for_idle())
        self.scanner.behavior[str(self.root_b)] = "success"
        generation_c = self.start(self.root_b)
        self.wait_active(generation_c)
        self.commit_activation(generation_c)
        self.scanner.behavior[str(self.root_c)] = "slow"
        self.scanner.release.clear()
        generation_d = self.start(self.root_c)
        self.broker.handle(request("cancel-d", "scanCancel", generationId=generation_d))
        self.assertTrue(self.broker.wait_for_idle())
        self.scanner.behavior[str(self.root_c)] = "success"
        generation_e = self.start(self.root_c)
        self.wait_active(generation_e)
        self.commit_activation(generation_e)
        self.assertEqual(len(list(self.broker.session_dir.glob("*.sqlite"))), 1)
        self.assertEqual(self.scanner.peak_active, 1)
        self.assertLessEqual(self.broker.high_water.outstanding_requests, 4)
        self.assertLessEqual(self.broker.high_water.scan_workers, 1)
        self.assertLessEqual(self.broker.high_water.search_workers, 1)
        self.assertLessEqual(self.broker.high_water.active_generations, 1)
        self.assertLessEqual(self.broker.high_water.staging_generations, 1)
        self.assertLessEqual(self.output.max_message_bytes, 1024 * 1024)

    def test_active_plus_staging_is_the_database_high_water(self):
        self.activate()
        self.scanner.release.clear()
        self.scanner.behavior[str(self.root_b)] = "slow"
        staging = self.start(self.root_b)
        self.assertTrue(self.scanner.started.wait(1))
        self.assertEqual(len(list(self.broker.session_dir.glob("*.sqlite"))), 2)
        self.assertEqual(self.broker.staging_generation_id, staging)
        self.scanner.release.set()
        self.wait_active(staging)
        self.assertEqual(len(list(self.broker.session_dir.glob("*.sqlite"))), 2)
        self.commit_activation(staging)
        self.assertEqual(len(list(self.broker.session_dir.glob("*.sqlite"))), 1)

    def test_broker_source_never_builds_snapshotbuilder_tree(self):
        source = (REPOSITORY / "helper/omatree_core/broker.py").read_text()
        self.assertNotIn("SnapshotBuilder", source)
        self.assertIn("writer.insert_directory", source)

    def test_shutdown_removes_active_database_and_session(self):
        self.activate()
        session = self.broker.session_dir
        self.broker.shutdown("stop")
        self.assertFalse(session.exists())
        self.assertEqual(self.messages()[-1]["type"], "shutdownComplete")

    def test_shutdown_cancels_staging_without_post_shutdown_events(self):
        self.scanner.behavior[str(self.root_a)] = "slow"
        self.start(self.root_a)
        self.assertTrue(self.scanner.started.wait(1))
        session = self.broker.session_dir
        self.broker.shutdown("stop-scan")
        self.assertFalse(session.exists())
        messages = self.messages()
        shutdown_index = next(
            index for index, message in enumerate(messages)
            if message["type"] == "shutdownComplete"
        )
        self.assertEqual(shutdown_index, len(messages) - 1)


class BrokerProtocolTests(unittest.TestCase):
    def valid_line(self, **changes):
        value = {
            "protocolVersion": BROKER_PROTOCOL_VERSION,
            "requestId": "r1",
            "operation": "hello",
        }
        value.update(changes)
        return json.dumps(value).encode() + b"\n"

    def test_parse_valid_and_reject_malformed_requests(self):
        self.assertEqual(parse_request_line(self.valid_line()).operation, "hello")
        for raw in (
            b"not-json\n", b"{}\n", self.valid_line(operation="unknown"),
            self.valid_line(requestId="bad id"), self.valid_line(protocolVersion=99),
        ):
            with self.assertRaises(BrokerProtocolError):
                parse_request_line(raw)
        with self.assertRaises(BrokerStreamError):
            parse_request_line(self.valid_line().rstrip(b"\n"))

    def test_oversized_request_and_response_are_rejected(self):
        with self.assertRaises(BrokerProtocolError):
            parse_request_line(b"x" * (MAX_REQUEST_LINE_BYTES + 1) + b"\n")
        with self.assertRaises(BrokerProtocolError):
            encode_message({"type": "x", "data": "x" * 65536})

    def test_framed_oversize_is_rejected_but_desynchronization_exits(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            runtime.chmod(0o700)
            shutdown = self.valid_line(requestId="stop", operation="shutdown")
            output = io.BytesIO()
            status = serve(
                io.BytesIO(b"x" * MAX_REQUEST_LINE_BYTES + b"\n" + shutdown),
                output, runtime_dir=runtime,
            )
            self.assertEqual(status, 0)
            self.assertTrue(any(
                json.loads(line).get("code") == "malformedRequest"
                for line in output.getvalue().splitlines()
            ))

        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            runtime.chmod(0o700)
            output = io.BytesIO()
            status = serve(
                io.BytesIO(b"x" * (MAX_REQUEST_LINE_BYTES + 1)),
                output, runtime_dir=runtime,
            )
            self.assertEqual(status, 2)
            self.assertEqual(list((runtime / "omatree").glob("session-*")), [])

    def test_eof_cleans_session_and_emits_no_traceback(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            runtime.chmod(0o700)
            output = io.BytesIO()
            self.assertEqual(serve(io.BytesIO(b""), output, runtime_dir=runtime), 0)
            messages = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(messages[0]["type"], "ready")
            self.assertEqual(messages[-1]["type"], "shutdownComplete")
            self.assertEqual(list((runtime / "omatree").glob("session-*")), [])
            self.assertNotIn(b"Traceback", output.getvalue())

    def test_session_paths_are_private_random_and_not_symlinked(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            runtime.chmod(0o700)
            first = create_broker_session(runtime)
            second = create_broker_session(runtime)
            self.assertNotEqual(first.name, second.name)
            self.assertEqual(first.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("root", first.name)
            first.rmdir()
            second.rmdir()

    def test_launcher_uses_absolute_python_and_protocol_smoke(self):
        launcher = REPOSITORY / "bin/omatree-broker"
        self.assertEqual(launcher.read_text().splitlines()[0], "#!/usr/bin/python3")
        with tempfile.TemporaryDirectory() as raw:
            Path(raw).chmod(0o700)
            environment = {**os.environ, "XDG_RUNTIME_DIR": raw, "PATH": "/nonexistent"}
            process = subprocess.Popen(
                [str(launcher)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=environment,
            )
            hello = self.valid_line(requestId="hello")
            shutdown = self.valid_line(requestId="stop", operation="shutdown")
            stdout, stderr = process.communicate(hello + shutdown, timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            types = [json.loads(line)["type"] for line in stdout.splitlines()]
            self.assertEqual(types, ["ready", "ready", "shutdownComplete"])
            self.assertNotIn(b"Traceback", stdout)


if __name__ == "__main__":
    unittest.main()
