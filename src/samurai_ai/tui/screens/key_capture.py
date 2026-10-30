"""Capture one key press for a rebind."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from samurai_ai.i18n import Translator


class KeyCaptureScreen(ModalScreen[str | None]):
    """Return the pressed key, or ``None`` on ``Esc``."""

    CSS = """
    KeyCaptureScreen {
        align: center middle;
    }
    #capture-dialog {
        width: 56;
        max-width: 96%;
        height: auto;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #capture-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #capture-current,
    #capture-hint {
        color: $text-dim;
    }
    """

    def __init__(self, translator: Translator, label: str, current: str) -> None:
        super().__init__()
        self.translator = translator
        self.label = label
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="capture-dialog"):
            yield Static(
                self.translator.t("keys.capture-title", action=self.label),
                id="capture-title",
            )
            yield Static(
                self.translator.t("keys.capture-current", pressed=self.current),
                id="capture-current",
            )
            yield Static(self.translator.t("keys.capture-hint"), id="capture-hint")

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
            return
        event.stop()
        self.dismiss(event.key)
