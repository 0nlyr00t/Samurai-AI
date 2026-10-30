"""Theme switching: the picker, and applying the operator's choice.

Built-in presets and theme files are offered together; the choice is written to
preferences so the next start comes up the same colour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from samurai_ai.tui import preferences
from samurai_ai.tui.screens import PickerScreen
from samurai_ai.tui.theme import BUILTIN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from samurai_ai.i18n import Translator

_LABELS = {
    "dark": "theme.label.dark",
    "light": "theme.label.light",
    "high-contrast": "theme.label.high-contrast",
}


class ThemeController:
    """Mixin: list the themes and switch to the one picked."""

    if TYPE_CHECKING:  # pragma: no cover - typing only
        theme: str
        translator: Translator
        _theme_files: list[str]

        def push_screen(self, screen: Any, callback: Any = None) -> Any: ...
        def _write_dim(self, text: str) -> None: ...
        def _write_line(self, text: str) -> None: ...

    def _theme_picks(self) -> list[tuple[str, str, str]]:
        t = self.translator.t
        picks = [
            (name, t(_LABELS[name]), t("theme.current") if name == self.theme else "")
            for name in BUILTIN
        ]
        picks.extend(
            (name, name, t("theme.current") if name == self.theme else "")
            for name in self._theme_files
        )
        return picks

    def _show_themes(self) -> None:
        self.push_screen(
            PickerScreen(
                self.translator,
                "theme.title",
                "theme.hint",
                "theme.empty",
                self._theme_picks(),
            ),
            self._on_theme_picked,
        )

    def _on_theme_picked(self, name: str | None) -> None:
        if name is None:
            return
        if name not in self.available_themes:  # type: ignore[attr-defined]
            self._write_dim(self.translator.t("theme.unknown", name=name))
            return
        self.theme = name
        try:
            preferences.save_theme(name)
        except OSError as error:
            self._write_dim(self.translator.t("theme.unsaved", error=str(error)))
            return
        self._write_line(self.translator.t("theme.applied", name=name))
