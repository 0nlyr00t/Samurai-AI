"""Frames: a closed, versioned vocabulary the carrier cannot fudge."""

import pytest

from samurai_ai.services.rpc.protocol import (
    PROTOCOL_VERSION,
    Hello,
    ProtocolError,
    Request,
    Response,
    decode,
    encode_line,
)


def test_a_request_round_trips() -> None:
    request = Request(id=7, method="operation.show", params={"operation_id": "op_1"})
    assert decode(request.as_frame()) == request
    assert encode_line(request.as_frame()).endswith(b"\n")


def test_the_handshake_carries_the_version() -> None:
    hello = decode({"hello": PROTOCOL_VERSION, "client": "attach", "read_only": True})
    assert isinstance(hello, Hello) and hello.read_only and hello.client == "attach"


def test_a_wrong_version_is_refused() -> None:
    with pytest.raises(ProtocolError, match="protocol mismatch"):
        decode({"hello": PROTOCOL_VERSION + 1})


def test_a_malformed_request_is_refused() -> None:
    with pytest.raises(ProtocolError, match="integer id"):
        decode({"id": "x", "method": "m"})
    with pytest.raises(ProtocolError, match="must be an object"):
        decode({"id": 1, "method": "m", "params": []})


def test_an_error_response_keeps_the_shape() -> None:
    frame = Response(id=3, error=None).as_frame()
    assert frame == {"id": 3, "result": None}
