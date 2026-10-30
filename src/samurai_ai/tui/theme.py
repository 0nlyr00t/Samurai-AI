"""Semantic colour tokens and the palettes that fill them.

Every colour the shell draws comes from a token, never from a hex literal in a
widget, so a theme can be swapped in one place. Textual resolves tokens for CSS
through ``Theme.variables``; Rich styles (journal lines, tag colours) resolve them
at write time through :meth:`App.token_color`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from textual.theme import Theme

#: Tokens every theme must fill. Ordered from the surface outwards.
TOKENS: tuple[str, ...] = (
    "surface",
    "panel",
    "panel-alt",
    "border",
    "text",
    "text-dim",
    "text-faint",
    "accent",
    "accent-alt",
    "success",
    "warning",
    "danger",
    "risk-low",
    "risk-mid",
    "risk-high",
)

#: Tokens that carry meaning for the operator; a theme may not leave them out.
REQUIRED: tuple[str, ...] = (
    "surface",
    "panel",
    "text",
    "text-dim",
    "accent",
    "success",
    "warning",
    "danger",
)

#: Text/background pairs whose contrast we report on load.
CONTRAST_PAIRS: tuple[tuple[str, str], ...] = (
    ("text", "surface"),
    ("text", "panel"),
    ("text-dim", "surface"),
    ("accent", "surface"),
)

MIN_CONTRAST = 3.0

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

BUILTIN: dict[str, dict[str, str]] = {
    "dark": {
        "surface": "#08090b",
        "panel": "#0a0a0a",
        "panel-alt": "#16213e",
        "border": "#30507a",
        "text": "#e6edf3",
        "text-dim": "#8b949e",
        "text-faint": "#6b7280",
        "accent": "#4fa8ff",
        "accent-alt": "#63e6be",
        "success": "#51cf66",
        "warning": "#ffd43b",
        "danger": "#ff6b6b",
        "risk-low": "#a9e34b",
        "risk-mid": "#ffa94d",
        "risk-high": "#ff6b6b",
    },
    "light": {
        "surface": "#f6f7f9",
        "panel": "#ffffff",
        "panel-alt": "#e8eef7",
        "border": "#9ab0c8",
        "text": "#12161b",
        "text-dim": "#4b5563",
        "text-faint": "#6b7280",
        "accent": "#1f6feb",
        "accent-alt": "#0b7285",
        "success": "#1a7f37",
        "warning": "#9a6700",
        "danger": "#b32222",
        "risk-low": "#1a7f37",
        "risk-mid": "#9a6700",
        "risk-high": "#b32222",
    },
    "high-contrast": {
        "surface": "#000000",
        "panel": "#000000",
        "panel-alt": "#1a1a1a",
        "border": "#ffffff",
        "text": "#ffffff",
        "text-dim": "#e0e0e0",
        "text-faint": "#c0c0c0",
        "accent": "#5bc8ff",
        "accent-alt": "#63e6be",
        "success": "#4ade4a",
        "warning": "#ffe066",
        "danger": "#ff5c5c",
        "risk-low": "#b6f06a",
        "risk-mid": "#ffb454",
        "risk-high": "#ff5c5c",
    },
}

DEFAULT_THEME = "dark"

THEME_SUFFIX = ".json"


class ThemeFileError(ValueError):
    """A theme file could not be turned into a theme."""


@dataclass(frozen=True)
class LoadedTheme:
    """A theme read from disk, with the problems it carries."""

    name: str
    palette: dict[str, str]
    warnings: tuple[str, ...]
    dark: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.palette)


def config_directory() -> Path:
    """Where themes and key bindings live: ``$XDG_CONFIG_HOME/samurai`` or the default."""
    override = os.environ.get("SAMURAI_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / "samurai"


def themes_directory() -> Path:
    return config_directory() / "themes"


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two hex colours, clamped to ``1.0 .. 21.0``."""
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def contrast_warnings(palette: dict[str, str]) -> tuple[str, ...]:
    """Report token pairs that are hard to read, without rejecting the theme."""
    warnings: list[str] = []
    for foreground, background in CONTRAST_PAIRS:
        first, second = palette.get(foreground), palette.get(background)
        if first is None or second is None:
            continue
        ratio = contrast_ratio(first, second)
        if ratio < MIN_CONTRAST:
            warnings.append(f"{foreground} on {background}: {ratio:.1f}:1")
    return tuple(warnings)


def build_theme(name: str, palette: dict[str, str], *, dark: bool) -> Theme:
    """Turn a palette into a Textual theme.

    Core slots are handed to Textual explicitly so its own widgets (inputs, option
    lists, scrollbars) pick up the palette instead of the framework default.
    """
    return Theme(
        name=name,
        dark=dark,
        primary=palette.get("accent", "$accent"),
        secondary=palette.get("accent-alt"),
        warning=palette.get("warning"),
        error=palette.get("danger"),
        success=palette.get("success"),
        foreground=palette.get("text"),
        background=palette.get("surface"),
        surface=palette.get("panel"),
        panel=palette.get("panel"),
        variables=dict(palette),
    )


def builtin_theme(name: str) -> Theme:
    return build_theme(name, BUILTIN[name], dark=name == "dark")


def clean_name(raw: str) -> str:
    """Normalise a theme name to the slug used by the file and the app."""
    slug = re.sub(r"[^a-z0-9]+", "-", Path(raw).stem.lower()).strip("-")
    if not slug:
        raise ThemeFileError(f"пустое имя темы: {raw!r}")
    return slug


def parse_palette(name: str, payload: str) -> LoadedTheme:
    """Read a theme file body.

    Unknown or misspelled tokens are errors — a typo would otherwise sit in the file
    doing nothing. Missing optional tokens fall back to the dark preset so a small
    file still renders.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ThemeFileError(f"{name}: не разобрать JSON ({error.msg})") from error
    if not isinstance(data, dict):
        raise ThemeFileError(f"{name}: ожидался объект токен → цвет")
    tokens = data.get("tokens", data)
    if not isinstance(tokens, dict):
        raise ThemeFileError(f"{name}: «tokens» должен быть объектом токен → цвет")

    palette: dict[str, str] = dict(BUILTIN[DEFAULT_THEME])
    supplied: dict[str, str] = {}
    for token, value in tokens.items():
        if token in {"name", "dark"}:
            continue
        if token not in TOKENS:
            raise ThemeFileError(f"{name}: неизвестный токен {token!r}")
        if not isinstance(value, str) or not _HEX.match(value):
            raise ThemeFileError(f"{name}: {token} = {value!r} — не цвет #rrggbb")
        supplied[token] = value.lower()

    missing = [token for token in REQUIRED if token not in supplied]
    if missing:
        raise ThemeFileError(f"{name}: нет обязательных токенов {', '.join(missing)}")
    palette.update(supplied)
    dark = data.get("dark", True)
    return LoadedTheme(
        name=name,
        palette=palette,
        warnings=contrast_warnings(palette),
        dark=dark if isinstance(dark, bool) else True,
    )


def load_theme_file(path: Path) -> LoadedTheme:
    return parse_palette(clean_name(path.name), path.read_text(encoding="utf-8"))


def load_theme_files(directory: Path) -> tuple[list[LoadedTheme], list[str]]:
    """Read every theme file in ``directory``; built-in names are never shadowed."""
    themes: list[LoadedTheme] = []
    problems: list[str] = []
    if not directory.is_dir():
        return themes, problems
    for path in sorted(directory.glob(f"*{THEME_SUFFIX}")):
        try:
            loaded = load_theme_file(path)
        except (OSError, ThemeFileError) as error:
            problems.append(str(error))
            continue
        if loaded.name in BUILTIN:
            problems.append(f"{loaded.name}: имя занято встроенной темой")
            continue
        themes.append(loaded)
        problems.extend(f"{loaded.name}: {warning}" for warning in loaded.warnings)
    return themes, problems


def _relative_luminance(color: str) -> float:
    red, green, blue = (_channel(color, offset) for offset in (1, 3, 5))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _channel(color: str, offset: int) -> float:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    raw = int(value[offset - 1 : offset + 1], 16) / 255
    return raw / 12.92 if raw <= 0.03928 else ((raw + 0.055) / 1.055) ** 2.4
