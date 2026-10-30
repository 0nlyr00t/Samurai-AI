"""Key reference overlay.

Content comes from the translator and is paged rather than dumped, so it stays
readable in an 80-column terminal.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from samurai_ai.i18n import Translator
from samurai_ai.tui.keys import display_key

_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "help.section.input",
        ("input-enter", "input-ctrlp", "input-help", "input-history", "input-shell"),
    ),
    ("help.section.streaming", ("stream-steer", "stream-stop")),
    ("help.section.panels", ("panel-keys", "panel-esc", "panel-expand")),
    ("help.section.boards", ("board-open", "board-actions", "board-filter")),
)


class HelpScreen(ModalScreen[None]):
    """What the keys do, grouped by the context they work in."""

    CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-dialog {
        width: 78;
        max-width: 96%;
        height: 80%;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #help-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #help-body {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }
    .help-section {
        color: $accent;
        padding-top: 1;
    }
    .help-line {
        color: $text;
    }
    #help-hint {
        padding-top: 1;
        color: $text-dim;
    }
    """

    BINDINGS = [
        Binding("escape", "close", show=False, priority=True),
        Binding("q", "close", show=False, priority=True),
        Binding("enter", "close", show=False, priority=True),
        Binding("pagedown", "page_down", show=False, priority=True),
        Binding("pageup", "page_up", show=False, priority=True),
    ]

    def __init__(self, translator: Translator, keymap: dict[str, str]) -> None:
        super().__init__()
        self.translator = translator
        self.keymap = keymap

    def _label(self, line: str) -> str:
        """Board keys come from the live map, so the sheet never lies."""
        if line != "board-open":
            return self.translator.t(f"help.line.{line}")
        return self.translator.t(
            f"help.line.{line}",
            tasks=display_key(self.keymap.get("open_task_board", "ctrl+t")),
            findings=display_key(self.keymap.get("open_finding_board", "ctrl+f")),
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static(self.translator.t("help.title"), id="help-title")
            with VerticalScroll(id="help-body"):
                for section_key, lines in _SECTIONS:
                    yield Static(self.translator.t(section_key), classes="help-section")
                    for line in lines:
                        yield Static(self._label(line), classes="help-line")
                yield Static(self.translator.t("help.section.commands"), classes="help-section")
                for line in self.translator.help_lines():
                    yield Static(line, classes="help-line")
            yield Static(self.translator.t("help.hint"), id="help-hint")

    def _body(self) -> VerticalScroll:
        return self.query_one("#help-body", VerticalScroll)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_page_down(self) -> None:
        self._body().scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self._body().scroll_page_up(animate=False)
