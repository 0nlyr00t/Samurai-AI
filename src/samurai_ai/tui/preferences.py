"""Where the operator's own look and bindings live.

One file per concern under the config directory, both read at start and written
by the in-app pickers, so the file and the UI can never disagree.
"""

from __future__ import annotations

import json
from pathlib import Path

from samurai_ai.tui.theme import config_directory

THEME_KEY = "theme"


class PreferenceError(ValueError):
    """A preferences file holds something we cannot read."""


def preferences_path() -> Path:
    return config_directory() / "preferences.json"


def keys_path() -> Path:
    return config_directory() / "keys.json"


def _read(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_theme() -> str | None:
    value = _read(preferences_path()).get(THEME_KEY)
    return value if isinstance(value, str) and value else None


def save_theme(name: str) -> None:
    data = _read(preferences_path())
    data[THEME_KEY] = name
    _write(preferences_path(), data)
