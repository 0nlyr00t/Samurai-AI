"""The RDP drivers' shared logic: the exit-code/log contract and the X.224
parsing that the sandbox actually runs.

The drivers execute as ``python3 -c`` inside the belt and cannot import
``samurai_ai``, so their logic travels as source in ``remote._RDP_SHARED``.
These tests ``exec`` that exact block and call its functions, so the contract is
pinned against the code that ships, not a copy.
"""

import socket
from collections.abc import Callable
from typing import Any

import pytest

from samurai_ai.execution import remote


@pytest.fixture(scope="module")
def shared() -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec("import os, re\n" + remote._RDP_SHARED, namespace)  # noqa: S102
    return namespace


@pytest.fixture
def verdict(shared: dict[str, Any]) -> Callable[[int, str], tuple[str, str]]:
    return shared["rdp_auth_verdict"]  # type: ignore[no-any-return]


# --- defect 2: xfreerdp /auth-only exit-code / log contract -----------------


def test_clean_exit_zero_is_auth_ok(verdict: Callable[[int, str], tuple[str, str]]) -> None:
    status, _ = verdict(0, "[INFO] connected\nAuthentication only, exit status 0\n")
    assert status == "AUTH_OK"


def test_connect_cancelled_is_never_auth_ok(
    verdict: Callable[[int, str], tuple[str, str]],
) -> None:
    """The exact field case: the wrapper read rc and said AUTH_OK while xfreerdp
    had cancelled the connection before any logon. That must be an error, not
    valid credentials."""
    log = (
        "[ERROR][com.freerdp.core] ERRCONNECT_CONNECT_CANCELLED [0x0002000B]\n"
        "Authentication only, exit status 1\n"
    )
    status, detail = verdict(1, log)
    assert status == "AUTH_ERROR"
    assert "CONNECT_CANCELLED" in detail


def test_error_blob_beats_exit_zero(
    verdict: Callable[[int, str], tuple[str, str]],
) -> None:
    """A build that prints a connect error yet exits 0 is still inconclusive —
    the last-error blob is authoritative, not the process rc."""
    status, _ = verdict(0, "ERRCONNECT_CONNECT_CANCELLED [0x0002000B]\n")
    assert status == "AUTH_ERROR"


@pytest.mark.parametrize(
    ("code", "name"),
    [
        (0x00020009, "AUTHENTICATION_FAILED"),
        (0x00020014, "LOGON_FAILURE"),
        (0x00020015, "WRONG_PASSWORD"),
        (0x00020016, "ACCESS_DENIED"),
        (0x00020018, "ACCOUNT_LOCKED_OUT"),
    ],
)
def test_credential_rejection_is_auth_fail(
    verdict: Callable[[int, str], tuple[str, str]], code: int, name: str
) -> None:
    status, detail = verdict(1, f"ERRCONNECT_{name} [0x{code:08x}]\n")
    assert status == "AUTH_FAIL"
    assert name in detail


@pytest.mark.parametrize(
    "code",
    [0x00020006, 0x00020008, 0x0002000C, 0x0002000D],
)
def test_connection_errors_do_not_test_credentials(
    verdict: Callable[[int, str], tuple[str, str]], code: int
) -> None:
    status, _ = verdict(1, f"ERRCONNECT_X [0x{code:08x}]\n")
    assert status == "AUTH_ERROR"


def test_success_blob_is_not_an_error(
    verdict: Callable[[int, str], tuple[str, str]],
) -> None:
    """ERRCONNECT_SUCCESS is 0x00000000 — it must not be read as a failure code."""
    status, _ = verdict(0, "ERRCONNECT_SUCCESS [0x00000000]\n")
    assert status == "AUTH_OK"


def test_nonzero_exit_without_a_code_is_inconclusive(
    verdict: Callable[[int, str], tuple[str, str]],
) -> None:
    status, _ = verdict(1, "some unrelated noise\n")
    assert status == "AUTH_ERROR"


def test_credential_code_wins_over_a_connection_code(
    verdict: Callable[[int, str], tuple[str, str]],
) -> None:
    log = "ERRCONNECT_CONNECT_CANCELLED [0x0002000B]\nERRCONNECT_LOGON_FAILURE [0x00020014]\n"
    status, detail = verdict(1, log)
    assert status == "AUTH_FAIL"
    assert "LOGON_FAILURE" in detail


# --- defect 3: X.224 negotiation parsing ------------------------------------


def _neg_rsp(protocol: int) -> bytes:
    # 7 bytes of X.224 CC, then RDP_NEG_RSP: type 2, flags 0, length 8, selected
    return b"\x00" * 7 + bytes((2, 0)) + (8).to_bytes(2, "little") + protocol.to_bytes(
        4, "little"
    )


def test_negotiation_tls(shared: dict[str, Any]) -> None:
    lines, selected = shared["rdp_parse_negotiation"](_neg_rsp(1))
    assert selected == 1
    assert "TLS" in lines[0]


def test_negotiation_nla(shared: dict[str, Any]) -> None:
    lines, selected = shared["rdp_parse_negotiation"](_neg_rsp(2))
    assert selected == 2
    assert "NLA" in lines[0]


def test_negotiation_standard_when_no_structure(shared: dict[str, Any]) -> None:
    lines, selected = shared["rdp_parse_negotiation"](b"\x00" * 7)
    assert selected == 0
    assert "standard RDP security" in lines[0]


def test_negotiation_failure(shared: dict[str, Any]) -> None:
    failure = b"\x00" * 7 + bytes((3, 0)) + (8).to_bytes(2, "little") + (6).to_bytes(
        4, "little"
    )
    lines, selected = shared["rdp_parse_negotiation"](failure)
    assert selected == -1
    assert "refused" in lines[0]


# --- the TCP short-read fix behind defect 3's flapping verdict --------------


def test_recv_exact_reassembles_a_split_header(shared: dict[str, Any]) -> None:
    """A header that arrives in two TCP segments must read as four bytes, not as
    a "short answer"."""
    left, right = socket.socketpair()
    try:
        right.sendall(b"\x03\x00")
        right.sendall(b"\x00\x13")
        header = shared["_rdp_recv_exact"](left, 4)
    finally:
        left.close()
        right.close()
    assert header == b"\x03\x00\x00\x13"


def test_recv_exact_returns_partial_on_eof(shared: dict[str, Any]) -> None:
    left, right = socket.socketpair()
    try:
        right.sendall(b"\x03")
        right.close()
        got = shared["_rdp_recv_exact"](left, 4)
    finally:
        left.close()
    assert got == b"\x03"


# --- defect 1: deterministic display selection ------------------------------


def test_pick_free_display_skips_taken_numbers(
    shared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(shared, "os", _FakeOs(["X99", "X100", "junk"]))
    assert shared["_rdp_pick_free_display"]() == 101


def test_pick_free_display_defaults_to_99(
    shared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(shared, "os", _FakeOs([]))
    assert shared["_rdp_pick_free_display"]() == 99


class _FakeOs:
    def __init__(self, entries: list[str]) -> None:
        self._entries = entries

    def listdir(self, _path: str) -> list[str]:
        return self._entries
