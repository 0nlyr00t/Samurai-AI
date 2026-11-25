"""Put text on the system clipboard through whatever the session can reach.

OSC 52 (sent by Textual) is the only path over SSH, but many terminals ignore it
(macOS Terminal.app, GNOME Terminal/VTE). So the app also tries a native tool —
chosen by what the session actually has, not just by what is installed: on an
X11 desktop with wl-clipboard installed, ``wl-copy`` exists but cannot connect,
and stopping at it used to lose the copy silently. Each candidate is tried in
turn until one exits cleanly; their stderr never reaches the TUI screen.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

Command = tuple[str, ...]

_TIMEOUT_SECONDS = 2


def native_commands(
    env: Mapping[str, str],
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Command]:
    """Installed clipboard tools that can work in this session, best first."""
    candidates: list[Command] = []
    if platform == "darwin":
        candidates.append(("pbcopy",))
    if platform.startswith(("win", "cygwin", "msys")):
        candidates.append(("clip",))
    if env.get("WAYLAND_DISPLAY"):
        candidates.append(("wl-copy",))
    if env.get("DISPLAY"):
        candidates += [("xclip", "-selection", "clipboard"), ("xsel", "--clipboard", "--input")]
    return [command for command in candidates if which(command[0])]


def copy_native(
    text: str,
    env: Mapping[str, str],
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> str | None:
    """Copy with the first tool that succeeds; its name, or None when none did."""
    data = text.encode("utf-8")
    for command in native_commands(env, platform, which):
        try:
            result = run(
                command,
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if getattr(result, "returncode", 1) == 0:
            return command[0]
    return None


__all__ = ["copy_native", "native_commands"]
