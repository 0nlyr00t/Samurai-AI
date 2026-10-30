"""Single-line prompt used when a palette action needs an argument."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from samurai_ai.i18n import Translator


class ArgumentScreen(ModalScreen[str | None]):
    """Ask for one value, in the same spirit as the palette's own field."""

    CSS = """
    ArgumentScreen {
        align: center middle;
    }
    #argument-dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #argument-label {
        color: $text;
        padding-bottom: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, translator: Translator, label: str) -> None:
        super().__init__()
        self.translator = translator
        self.label = label

    def compose(self) -> ComposeResult:
        with Vertical(id="argument-dialog"):
            yield Static(self.label, id="argument-label")
            yield Input(placeholder=self.translator.t("palette.placeholder"), id="argument-input")

    def on_mount(self) -> None:
        self.query_one("#argument-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
