"""A refusal that names the resource and offers it to the operator.

The guard stopping a command is information, not a failure — but it used to be a
dead end that cost a model round trip while the operator retyped the host. The
panel sits at the top, where the refusal is read, and Enter turns it into a
scope rule and runs the command again.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from samurai_ai.i18n import Translator


class ScopeAskScreen(ModalScreen[bool]):
    """Enter: add to the scope and retry. `s` or Esc: skip the command."""

    BINDINGS = [
        ("escape", "skip", "skip"),
        ("s", "skip", "skip"),
        ("enter", "accept", "add"),
    ]

    CSS = """
    ScopeAskScreen {
        align: center top;
    }
    #scope-ask {
        width: 78;
        height: auto;
        margin-top: 2;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #scope-ask-title {
        color: $accent;
        text-style: bold;
    }
    #scope-ask-rule {
        padding: 1 0;
        color: $text;
    }
    #scope-ask-hint {
        color: $text-dim;
    }
    """

    def __init__(self, translator: Translator, literal: str, detail: str) -> None:
        super().__init__()
        self.translator = translator
        self.literal = literal
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="scope-ask"):
            yield Static(self.translator.t("scope.ask-title"), id="scope-ask-title")
            yield Static(f"◌ {self.literal}", id="scope-ask-rule")
            yield Static(self.detail, id="scope-ask-why")
            yield Static(self.translator.t("scope.ask-hint"), id="scope-ask-hint")

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_skip(self) -> None:
        self.dismiss(False)
