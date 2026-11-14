"""Contextual key hints for the chrome below the command input.

The hint bar is another view of the live keymap, like the palette and the key
reference. Modal screens may expose ``HINT_KEYS`` as ``(key, translation_key)``
pairs; the generic Enter/Esc pair keeps every other modal predictable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from rich.text import Text
from textual.screen import ModalScreen, Screen

from samurai_ai.tui import keys

IDLE_ACTIONS = ("command_palette", "retry_turn", "show_help")
RUNNING_ACTIONS = ("quit_requested", "steer", "shell_background")
GENERIC_MODAL_HINTS = (("enter", "hint.select"), ("escape", "hint.close"))

Translate = Callable[..., str]


def context_for(screen: Screen[Any], *, chat_busy: bool) -> str:
    """Which set of keys answers the operator's current situation."""
    if isinstance(screen, ModalScreen):
        return "modal"
    return "running" if chat_busy else "idle"


def _fit(parts: list[str], width: int) -> str:
    """Join as many complete hints as fit; never leak a clipped final word."""
    if width <= 0:
        return ""
    chosen: list[str] = []
    used = 0
    for part in parts:
        extra = Text(part).cell_len + (3 if chosen else 0)  # " · "
        if chosen and used + extra > width:
            break
        chosen.append(part)
        used += extra
    text = " · ".join(chosen)
    if len(chosen) < len(parts) and Text(text).cell_len + 4 <= width:
        text += " · …"
    if Text(text).cell_len <= width:
        return text
    clipped = Text(text)
    clipped.truncate(width, overflow="ellipsis")
    return str(clipped)


def render_hints(
    context: str,
    keymap: dict[str, str],
    translate: Translate,
    *,
    width: int,
    modal_hints: Iterable[tuple[str, str]] = GENERIC_MODAL_HINTS,
    shell_running: bool = False,
) -> str:
    """Render the useful keys for one context, localized and width-bounded."""
    if context == "modal":
        parts = [
            f"{keys.display_key(key)} {translate(label_key)}"
            for key, label_key in modal_hints
        ]
        return _fit(parts, width)
    actions = list(RUNNING_ACTIONS if context == "running" else IDLE_ACTIONS)
    if context == "running" and not shell_running:
        actions.remove("shell_background")
    parts = []
    for action in actions:
        key = keymap.get(action)
        if key:
            parts.append(f"{keys.display_key(key)} {translate(keys.label_key(action))}")
    return _fit(parts, width)
