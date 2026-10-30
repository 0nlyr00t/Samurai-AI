"""Tool run inspector: everything known about one governed run.

The screen is dumb on purpose — the app builds the sections and hands over the
callbacks, so the screen owns only layout and keys.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from samurai_ai.i18n import Translator

Section = tuple[str, Sequence[str]]


class InspectorScreen(ModalScreen[None]):
    """Read-only view of a run: definition, scope, egress, artifacts, output."""

    CSS = """
    InspectorScreen {
        align: center middle;
    }
    #inspector-dialog {
        width: 92;
        max-width: 98%;
        height: 85%;
        padding: 1 2;
        border: solid #5bc0be;
        background: #0a0a0a;
    }
    #inspector-title {
        text-style: bold;
        color: #5bc0be;
        padding-bottom: 1;
    }
    #inspector-body {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }
    .inspector-section {
        color: #b197fc;
        padding-top: 1;
    }
    .inspector-line {
        color: #c9d1d9;
    }
    #inspector-hint {
        padding-top: 1;
        color: #7d8590;
    }
    """

    BINDINGS = [
        Binding("escape", "close", show=False, priority=True),
        Binding("q", "close", show=False, priority=True),
        Binding("pagedown", "page_down", show=False, priority=True),
        Binding("pageup", "page_up", show=False, priority=True),
    ]

    def __init__(
        self,
        translator: Translator,
        title: str,
        sections: Sequence[Section],
        actions: dict[str, Callable[[], None]],
    ) -> None:
        super().__init__()
        self.translator = translator
        self.title_text = title
        self.sections = sections
        self.actions = actions
        for key in actions:
            self._bindings.bind(key, f"act('{key}')", show=False, priority=True)

    def compose(self) -> ComposeResult:
        with Vertical(id="inspector-dialog"):
            yield Static(self.title_text, id="inspector-title")
            with VerticalScroll(id="inspector-body"):
                for heading, lines in self.sections:
                    yield Static(heading, classes="inspector-section")
                    for line in lines or (self.translator.t("inspector.none"),):
                        yield Static(f"  {line}", classes="inspector-line", markup=False)
            yield Static(
                self.translator.t(
                    "inspector.hint",
                    keys=" ".join(k.upper() for k in self.actions),
                ),
                id="inspector-hint",
            )

    def action_close(self) -> None:
        self.dismiss(None)

    def action_page_down(self) -> None:
        self.query_one("#inspector-body", VerticalScroll).scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self.query_one("#inspector-body", VerticalScroll).scroll_page_up(animate=False)

    def action_act(self, key: str) -> None:
        handler = self.actions.get(key)
        if handler is None:
            return
        handler()
        self.dismiss(None)
