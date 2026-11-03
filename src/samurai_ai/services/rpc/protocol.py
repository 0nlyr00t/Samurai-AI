"""Wire vocabulary: JSON lines, one frame per line, versioned.

The same frames serve both carriers — in-process (no serialization) and the unix
socket — so "remote" is a different carrier, not a different code path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1 << 20


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    id: int
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    def as_frame(self) -> dict[str, Any]:
        return {"id": self.id, "method": self.method, "params": self.params}


@dataclass(frozen=True)
class Response:
    id: int
    result: Any = None
    error: ErrorBody | None = None

    def as_frame(self) -> dict[str, Any]:
        frame: dict[str, Any] = {"id": self.id}
        if self.error is not None:
            frame["error"] = {"code": self.error.code, "message": self.error.message}
        else:
            frame["result"] = self.result
        return frame


@dataclass(frozen=True)
class ErrorBody:
    code: str
    message: str


@dataclass(frozen=True)
class Event:
    seq: int
    operation_id: str
    event_type: str
    payload: dict[str, Any]

    def as_frame(self) -> dict[str, Any]:
        return {
            "event": self.event_type,
            "seq": self.seq,
            "operation_id": self.operation_id,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class Hello:
    """First frame each way: the peer says who and what it is."""

    protocol: int = PROTOCOL_VERSION
    client: str = "unknown"
    read_only: bool = False

    def as_frame(self) -> dict[str, Any]:
        return {
            "hello": self.protocol,
            "client": self.client,
            "read_only": self.read_only,
        }


def decode(frame: dict[str, Any]) -> Request | Hello:
    if "hello" in frame:
        version = frame.get("hello")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"protocol mismatch: peer speaks {version}, this side speaks "
                f"{PROTOCOL_VERSION}"
            )
        return Hello(
            protocol=PROTOCOL_VERSION,
            client=str(frame.get("client") or "unknown"),
            read_only=bool(frame.get("read_only")),
        )
    raw_id = frame.get("id")
    method = frame.get("method")
    if not isinstance(raw_id, int) or not isinstance(method, str):
        raise ProtocolError("request frame needs an integer id and a method")
    params = frame.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError("request params must be an object")
    return Request(id=raw_id, method=method, params=params)


def encode_line(frame: dict[str, Any]) -> bytes:
    line = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame is {len(line)} bytes, over the limit")
    return line + b"\n"


__all__ = [
    "PROTOCOL_VERSION",
    "ErrorBody",
    "Event",
    "Hello",
    "ProtocolError",
    "Request",
    "Response",
    "decode",
    "encode_line",
]
