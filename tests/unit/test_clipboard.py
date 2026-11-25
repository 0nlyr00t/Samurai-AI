"""The native clipboard path picks tools the session can reach and falls through."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from samurai_ai.tui.clipboard import copy_native, native_commands


def _every_tool(name: str) -> str:
    return f"/usr/bin/{name}"


def test_tools_follow_the_session_not_just_what_is_installed() -> None:
    assert native_commands({}, "linux", _every_tool) == []  # SSH: OSC 52 only
    x11 = native_commands({"DISPLAY": ":0"}, "linux", _every_tool)
    assert [command[0] for command in x11] == ["xclip", "xsel"]
    both = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    wayland = native_commands(both, "linux", _every_tool)
    assert [command[0] for command in wayland] == ["wl-copy", "xclip", "xsel"]
    assert native_commands({}, "darwin", _every_tool) == [("pbcopy",)]
    assert native_commands({"DISPLAY": ":0"}, "linux", lambda name: None) == []


def test_a_failing_tool_falls_through_to_the_next() -> None:
    """wl-copy installed on X11 used to end the search and lose the copy."""
    calls: list[str] = []

    def run(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(command[0])
        assert kwargs["stderr"] is subprocess.DEVNULL  # never painted over the TUI
        if command[0] == "wl-copy":
            return SimpleNamespace(returncode=1)
        if command[0] == "xclip":
            raise subprocess.TimeoutExpired(command, 2)
        return SimpleNamespace(returncode=0)

    used = copy_native(
        "text",
        {"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"},
        platform="linux",
        which=_every_tool,
        run=run,
    )
    assert used == "xsel"
    assert calls == ["wl-copy", "xclip", "xsel"]


def test_nothing_reachable_is_reported_as_none() -> None:
    def run(command, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("gone")

    assert copy_native("t", {"DISPLAY": ":0"}, platform="linux", which=_every_tool, run=run) is None
    assert copy_native("t", {}, platform="linux", which=_every_tool) is None
