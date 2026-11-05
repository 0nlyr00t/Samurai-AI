"""The relay runner: a quoted one-liner, no container, same validation."""

from pathlib import Path

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
