"""The ``/keys`` screen: show every action's key and rebind one.

A conflict is refused with the name of the action that already owns the key, so
the operator decides which of the two moves. Every change is written to the same
file the start-up loader reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from samurai_ai.tui import keys, preferences
from samurai_ai.tui.screens import KeyCaptureScreen, PickerScreen

if TYPE_CHECKING:  # pragma: no cover - typing only
    from samurai_ai.i18n import Translator


class KeyMapController:
    """Mixin: list the bindings and rebind one of them."""

    if TYPE_CHECKING:  # pragma: no cover - typing only
        keymap: dict[str, str]
        translator: Translator

        def push_screen(self, screen: Any, callback: Any = None) -> Any: ...
        def _write_dim(self, text: str) -> None: ...
        def _write_line(self, text: str) -> None: ...
        def _refresh_keymap(self) -> None: ...
        _key_capture: bool

    async def _check_bindings(self, key: str, priority: bool = False) -> bool:
        """While capturing, no binding may fire — the pressed key is the answer.

        Textual checks bindings before the screen sees the key, so a rebind to
        ``ctrl+t`` would otherwise open the boards instead of being recorded.
        Overriding the check is the one hook that runs first.
        """
        if self._key_capture:
            return False
        handled: bool = await super()._check_bindings(key, priority)  # type: ignore[misc]
        return handled

    def _key_picks(self) -> list[tuple[str, str, str]]:
        return [
            (
                action.name,
                keys.display_key(self.keymap[action.name]),
                self.translator.t(action.label_key),
            )
            for action in keys.ACTIONS
        ]

    def _show_keys(self) -> None:
        self.push_screen(
            PickerScreen(
                self.translator,
                "keys.title",
                "keys.hint",
                "keys.empty",
                self._key_picks(),
            ),
            self._on_key_action_picked,
        )

    def _on_key_action_picked(self, action: str | None) -> None:
        if action is None or not keys.is_known_action(action):
            return
        label = self.translator.t(keys.label_key(action))
        self._key_capture = True
        self.push_screen(
            KeyCaptureScreen(self.translator, label, self.keymap.get(action, "")),
            lambda pressed: self._on_key_captured(action, pressed),
        )

    def _on_key_captured(self, action: str, pressed: str | None) -> None:
        self._key_capture = False
        if pressed is None:
            return
        try:
            canonical = keys.normalize(pressed)
        except keys.KeyMapError as error:
            self._write_dim(self.translator.t("keys.invalid", pressed=pressed, error=str(error)))
            return
        taken = keys.owner(canonical, self.keymap, except_action=action)
        label = self.translator.t(keys.label_key(action))
        if taken is not None:
            self._write_dim(
                self.translator.t(
                    "keys.conflict",
                    pressed=canonical,
                    owner=self.translator.t(keys.label_key(taken)),
                    action=label,
                )
            )
            self._show_keys()
            return
        updated = {**self.keymap, action: canonical}
        try:
            keys.save(preferences.keys_path(), updated)
        except OSError as error:
            self._write_dim(self.translator.t("keys.unsaved", error=str(error)))
            return
        self.keymap = updated
        self._refresh_keymap()
        self._write_line(self.translator.t("keys.bound", action=label, pressed=canonical))
        self._show_keys()
