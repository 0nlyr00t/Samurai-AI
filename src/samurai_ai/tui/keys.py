"""The key map: defaults, the operator's overrides, and conflict checks.

One list defines every rebindable action, so the bindings, the ``/keys`` screen
and the help overlay cannot drift. A binding lands on exactly one action: two
actions may never claim the same key, and a conflicting override is refused with
the name of the action that already owns it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from textual.binding import Binding
from textual.keys import Keys

_MODIFIERS = frozenset({"ctrl", "alt", "shift", "super", "meta", "hyper"})
_KEY_NAMES = frozenset(member.value for member in Keys)


@dataclass(frozen=True)
class Action:
    """One rebindable global action."""

    name: str
    key: str
    label_key: str
    priority: bool = False


#: Every global action the operator may rebind, with its default key.
ACTIONS: tuple[Action, ...] = (
    Action("quit_requested", "ctrl+c", "keys.action.quit"),
    Action("toggle_journal", "ctrl+o", "keys.action.journal"),
    Action("command_palette", "ctrl+p", "keys.action.palette", priority=True),
    Action("show_help", "f1", "keys.action.help"),
    Action("inspect_last_run", "ctrl+i", "keys.action.inspect", priority=True),
    Action("open_sessions", "ctrl+g", "keys.action.sessions", priority=True),
    Action("open_task_board", "ctrl+t", "keys.action.tasks", priority=True),
    Action("attach_session", "ctrl+d", "keys.action.attach", priority=True),
    Action("open_finding_board", "ctrl+f", "keys.action.findings", priority=True),
    Action("steer", "ctrl+s", "keys.action.steer", priority=True),
    Action("shell_background", "ctrl+b", "keys.action.shell"),
    Action("expand_output", "ctrl+e", "keys.action.expand", priority=True),
    Action("toggle_plan", "shift+tab", "keys.action.plan", priority=True),
    Action("retry_turn", "ctrl+r", "keys.action.retry", priority=True),
    Action("op_next", "ctrl+tab", "keys.action.op-next", priority=True),
    Action("op_previous", "ctrl+shift+tab", "keys.action.op-previous", priority=True),
    Action("op_close", "ctrl+w", "keys.action.op-close", priority=True),
    Action("toggle_follow", "ctrl+t", "keys.action.follow", priority=True),
    Action("journal_search", "ctrl+/", "keys.action.search", priority=True),
    Action("journal_next", "ctrl+n", "keys.action.search-next", priority=True),
    Action("journal_previous", "ctrl+shift+n", "keys.action.search-prev", priority=True),
    Action("view_journal", "ctrl+1", "keys.action.view-journal", priority=True),
    Action("view_ops", "ctrl+2", "keys.action.view-ops", priority=True),
    Action("view_bounty", "ctrl+3", "keys.action.view-bounty", priority=True),
    Action("view_tasks", "ctrl+4", "keys.action.view-tasks", priority=True),
    Action("toggle_side", "ctrl+6", "keys.action.side", priority=True),
    Action("view_next", "ctrl+right", "keys.action.view-next", priority=True),
    Action("view_previous", "ctrl+left", "keys.action.view-previous", priority=True),
)

_BY_NAME = {action.name: action for action in ACTIONS}


class KeyMapError(ValueError):
    """A key string cannot be parsed."""


def is_known_action(name: str) -> bool:
    return name in _BY_NAME


def label_key(name: str) -> str:
    """Translation key of an action's label, or ``""`` when the action is unknown."""
    action = _BY_NAME.get(name)
    return action.label_key if action is not None else ""


def parse_key(key: str) -> tuple[list[str], str]:
    """Split and validate a key string such as ``ctrl+shift+tab``."""
    raw = key.strip().lower()
    if not raw or " " in raw:
        raise KeyMapError(f"пустая или составная клавиша: {key!r}")
    modifiers, base = Binding(raw, "x").parse_key()
    unknown = [modifier for modifier in modifiers if modifier not in _MODIFIERS]
    if unknown:
        raise KeyMapError(f"{key!r}: неизвестный модификатор {unknown[0]}")
    if not base:
        raise KeyMapError(f"{key!r}: нет самой клавиши")
    if len(base) > 1 and base not in _KEY_NAMES:
        raise KeyMapError(f"{key!r}: неизвестная клавиша {base!r}")
    return modifiers, base


def display_key(key: str) -> str:
    """Readable form for the key sheet: ``ctrl+shift+tab`` → ``Ctrl+Shift+Tab``."""
    try:
        modifiers, base = parse_key(key)
    except KeyMapError:
        return key
    parts = [modifier.capitalize() for modifier in sorted(set(modifiers))]
    return "+".join([*parts, base.capitalize() if len(base) > 1 else base.upper()])


def normalize(key: str) -> str:
    """Canonical form, so ``Ctrl+T`` and ``ctrl+t`` occupy the same slot."""
    modifiers, base = parse_key(key)
    return "+".join([*sorted(set(modifiers)), base])


def default_map() -> dict[str, str]:
    return {action.name: action.key for action in ACTIONS}


def owner(key: str, mapping: dict[str, str], *, except_action: str = "") -> str | None:
    """The action currently holding ``key``, if any."""
    wanted = normalize(key)
    for name, bound in mapping.items():
        if name == except_action:
            continue
        try:
            if normalize(bound) == wanted:
                return name
        except KeyMapError:
            continue
    return None


def parse_override(payload: str) -> tuple[dict[str, str], list[str]]:
    """Read a ``keys.json`` body into a partial map plus problems.

    A conflicting or unknown entry is skipped rather than fatal: one bad line must
    not cost the operator every other binding.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise KeyMapError(f"keys.json: не разобрать JSON ({error.msg})") from error
    if not isinstance(data, dict):
        raise KeyMapError("keys.json: ожидался объект действие → клавиша")

    overrides: dict[str, str] = {}
    problems: list[str] = []
    for name, key in data.items():
        if not is_known_action(name):
            problems.append(f"{name}: неизвестное действие")
            continue
        if not isinstance(key, str):
            problems.append(f"{name}: клавиша должна быть строкой")
            continue
        try:
            canonical = normalize(key)
        except KeyMapError as error:
            problems.append(str(error))
            continue
        taken = owner(canonical, {**default_map(), **overrides})
        if taken is not None and taken != name:
            problems.append(f"{canonical}: занято действием {taken}")
            continue
        overrides[name] = canonical
    return overrides, problems


def load(path: Path) -> tuple[dict[str, str], list[str]]:
    """Effective map (defaults plus overrides) and the problems found."""
    mapping = default_map()
    if not path.is_file():
        return mapping, []
    try:
        overrides, problems = parse_override(path.read_text(encoding="utf-8"))
    except (OSError, KeyMapError) as error:
        return mapping, [str(error)]
    mapping.update(overrides)
    return mapping, problems


def save(path: Path, overrides: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(overrides, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")


def to_bindings(mapping: dict[str, str], labels: dict[str, str]) -> list[Binding]:
    """Textual bindings for the effective map, in list order."""
    return [
        Binding(
            mapping.get(action.name, action.key),
            action.name,
            labels.get(action.name, action.label_key),
            show=False,
            priority=action.priority,
        )
        for action in ACTIONS
    ]
