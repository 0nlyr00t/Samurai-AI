"""The relay runner: a quoted one-liner, no container, same validation."""

from pathlib import Path
from typing import Any

import pytest

from samurai_ai.domain.execution import ToolValidationError
from samurai_ai.execution.relay import (
    MAX_RELAY_LENGTH,
    RelaySandboxRunner,
    relay_command,
    validate_relay,
)
from samurai_ai.execution.sandbox import get_profile


def runner(relay: str = "prlctl exec '{uuid}'") -> RelaySandboxRunner:
    return RelaySandboxRunner(_Manager(), Path("/tmp/s.sig"), bytes, relay)


class _Manager:
    stall_seconds = 120.0


def test_the_command_is_one_quoted_line() -> None:
    line = relay_command("ssh -J jump", "/usr/bin/nmap", ["-p", "80,443", "10.0.0.1"])
    assert line == "ssh -J jump /usr/bin/nmap -p 80,443 10.0.0.1"


def test_a_hostile_argument_stays_one_argument() -> None:
    line = relay_command("relay", "curl", ["http://x/?a=1; rm -rf /tmp/x", "$(id)"])
    assert "'; rm -rf /tmp/x'" in line or "'http://x/?a=1; rm -rf /tmp/x'" in line
    assert "'$(id)'" in line


def test_build_argv_wraps_the_tool_instead_of_docker() -> None:
    argv = runner().build_argv(get_profile("sandbox.nmap@v1"), "run1", "nmap", ["-sT", "10.0.0.1"])
    import shlex

    assert argv[:2] == ["bash", "-lc"]
    assert shlex.split(argv[2]) == ["prlctl", "exec", "{uuid}", "nmap", "-sT", "10.0.0.1"]


def test_the_profile_allowlist_still_applies() -> None:
    with pytest.raises(ToolValidationError, match="not in sandbox profile"):
        runner().build_argv(get_profile("sandbox.nmap@v1"), "r", "ffuf", ["-u", "x"])


def test_a_bad_prefix_is_refused() -> None:
    with pytest.raises(ToolValidationError, match="empty"):
        validate_relay("  ")
    with pytest.raises(ToolValidationError, match="NUL"):
        validate_relay("a\x00b")
    with pytest.raises(ToolValidationError, match="longer than"):
        validate_relay("x" * (MAX_RELAY_LENGTH + 1))
    assert validate_relay("  ssh -J jump  ") == "ssh -J jump"


def test_preflight_does_not_need_a_local_image() -> None:
    import asyncio

    from samurai_ai.domain.sandbox import SandboxProfile

    class _NoSignature(RelaySandboxRunner):
        def verify_allowlist_signature(self) -> str | None:  # noqa: D102 - stub
            return None

    quiet = _NoSignature(_Manager(), Path("/tmp/x.sig"), bytes, "relay")  # type: ignore[arg-type]
    profile = SandboxProfile(id="sandbox.probe@v1", image="nothing@sha256:" + "a" * 64,
                             allowed_executables=("nmap",), network_mode="direct")
    assert asyncio.run(quiet.preflight(profile)) is None


def test_a_relayed_fetch_parses_status_and_body() -> None:
    from samurai_ai.execution.relay import relayed_fetch

    class _Manager:
        stall_seconds = 120.0

        async def run(self, argv, timeout_seconds=0, **_: Any):  # noqa: ANN001, ANN003
            assert argv[0] == "bash" and argv[1] == "-lc"
            assert "curl" in argv[2] and "http://10.0.0.1/x" in argv[2]
            return type(
                "R", (), {"stdout": "HTTP/1.1 200 OK\r\nX-A: 1\r\n\r\nbody-here", "stderr": "",
                          "exit_code": 0, "timed_out": False}
            )()

    response = relayed_fetch(
        "relay", "http://10.0.0.1/x", method="GET", timeout=5, max_bytes=1024,
        manager=_Manager(),
    )
    assert response.status == 200
    assert response.body == b"body-here"
    assert "X-A: 1" in response.headers


def test_a_failed_relayed_fetch_says_what_curl_said() -> None:
    from samurai_ai.domain.execution import ToolExecutionError
    from samurai_ai.execution.relay import relayed_fetch

    class _Manager:
        stall_seconds = 120.0

        async def run(self, *_a: Any, **_k: Any):
            return type(
                "R", (), {"stdout": "", "stderr": "curl: (7) Failed to connect",
                          "exit_code": 7, "timed_out": False}
            )()

    with pytest.raises(ToolExecutionError, match="Failed to connect"):
        relayed_fetch(
            "relay", "http://10.0.0.1/x", method="GET", timeout=5, max_bytes=1024,
            manager=_Manager(),
        )


def test_the_probe_reports_what_the_far_side_has() -> None:
    from samurai_ai.execution import relay as module

    class _Manager:
        async def run(self, argv, timeout_seconds=0, **_k):  # noqa: ANN001, ANN003
            return type(
                "R", (), {"stdout": "nmap\npython3\n", "stderr": "", "exit_code": 0,
                          "timed_out": False}
            )()

    from samurai_ai.execution import process as process_module

    original = process_module.ProcessManager
    process_module.ProcessManager = lambda **_k: _Manager()  # type: ignore[assignment]
    try:
        found = module.probe_tools("relay", ("nmap", "nuclei", "python3"))
    finally:
        process_module.ProcessManager = original  # type: ignore[assignment]
    assert found == {"nmap": True, "nuclei": False, "python3": True}
