"""Bounded control protocol for the future OmaTree Quickshell frontend."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping


BROKER_PROTOCOL_VERSION = 1
MAX_REQUEST_LINE_BYTES = 64 * 1024
MAX_INPUT_BUFFER_BYTES = 256 * 1024
MAX_OUTSTANDING_REQUESTS = 4
MAX_CONTROL_MESSAGE_BYTES = 64 * 1024
MAX_QUERY_MESSAGE_BYTES = 1024 * 1024
MAX_REQUEST_ID_BYTES = 128
MAX_GENERATION_ID_BYTES = 128
MAX_CONTINUATION_BYTES = 32 * 1024

OPERATIONS = frozenset({
    "hello", "scanStart", "scanCancel", "metadata", "children",
    "ancestors", "search", "childrenAt", "activationCommit", "activationAbort", "shutdown",
})
QUERY_OPERATIONS = frozenset({"metadata", "children", "childrenAt", "ancestors", "search"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]+$")


class BrokerProtocolError(ValueError):
    """A single broker request violates the bounded protocol contract."""


class BrokerStreamError(BrokerProtocolError):
    """Input framing is no longer trustworthy and the broker must exit."""


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    request_id: str
    operation: str
    values: Mapping[str, Any]


def _encoded_size(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="surrogateescape"))
    except UnicodeEncodeError as error:
        raise BrokerProtocolError("request string is not safely encodable") from error


def validate_identifier(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str) or not value
        or _encoded_size(value) > maximum or _IDENTIFIER.fullmatch(value) is None
    ):
        raise BrokerProtocolError(f"invalid {label}")
    return value


def parse_request_line(raw: bytes) -> BrokerRequest:
    if len(raw) > MAX_REQUEST_LINE_BYTES:
        raise BrokerProtocolError("request line exceeds 65536 bytes")
    if not raw.endswith(b"\n"):
        raise BrokerStreamError("unterminated request frame")
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise BrokerProtocolError("request is not valid JSON") from error
    if not isinstance(value, dict):
        raise BrokerProtocolError("request must be a JSON object")
    if value.get("protocolVersion") != BROKER_PROTOCOL_VERSION:
        raise BrokerProtocolError("unsupported broker protocol version")
    request_id = validate_identifier(
        value.get("requestId"), "request ID", MAX_REQUEST_ID_BYTES
    )
    operation = value.get("operation")
    if operation not in OPERATIONS:
        raise BrokerProtocolError("unknown broker operation")
    return BrokerRequest(request_id, operation, value)


def encode_message(message: Mapping[str, Any], *, query: bool = False) -> bytes:
    payload = {"protocolVersion": BROKER_PROTOCOL_VERSION, **message}
    try:
        encoded = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise BrokerProtocolError("broker response is not encodable") from error
    maximum = MAX_QUERY_MESSAGE_BYTES if query else MAX_CONTROL_MESSAGE_BYTES
    if len(encoded) > maximum:
        raise BrokerProtocolError("broker response exceeds encoded limit")
    return encoded
