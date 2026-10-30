"""Sessions screens: the child list and one child's own feed.

Both screens are dumb — the app builds the rows and sections and supplies the
callbacks, so nothing here touches a service.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator

Row = tuple[str, str]  # (run id, one-line label)


class SessionsScreen(ModalScreen[str]):
    """List of child sessions; Enter opens one, ``x`` stops it."""

    BINDINGS = [
        Binding("escape", "dismiss", "close", priority=True),
        Binding("q", "dismiss", "close", show=False),
    ]

    CSS = """
    SessionsScreen {
        align: center middle;
    }
    #sessions-dialog {
        width: 78;
        max-width: 96%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: solid $accent-alt;
        background: $panel;
    }
    #sessions-title {
        text-style: bold;
        color: $accent-alt;
    }
    #sessions-hint {
        color: $text-dim;
        padding-bottom: 1;
    }
    #sessions-list {
        height: auto;
        max-height: 20;
        background: $panel;
    }
    #sessions-empty {
        color: $text-dim;
        height: auto;
    }
    """

    def __init__(
        self,
        translator: Translator,
        rows: list[Row],
        on_stop: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.translator = translator
        self.rows = rows
        self.on_stop = on_stop

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="sessions-dialog"):
            yield Static(self.translator.t("sessions.title"), id="sessions-title")
            yield Static(self.translator.t("sessions.hint"), id="sessions-hint")
            if not self.rows:
                yield Static(self.translator.t("sessions.empty"), id="sessions-empty")
            else:
                yield OptionList(
                    *(Option(label, id=run_id) for run_id, label in self.rows),
                    id="sessions-list",
                )

    def on_mount(self) -> None:
        if self.rows:
            listed = self.query_one("#sessions-list", OptionList)
            listed.highlighted = 0
            listed.focus()

    def selected(self) -> str | None:
        if not self.rows:
            return None
        option = self.query_one("#sessions-list", OptionList).highlighted
        if option is None:
            return None
        return self.rows[option][0]

    def on_key(self, event: Any) -> None:
        if event.key == "enter":
            run_id = self.selected()
            if run_id is not None:
                self.dismiss(run_id)
        elif event.key == "x":
            run_id = self.selected()
            if run_id is not None:
                self.on_stop(run_id)
                self.dismiss(None)
        elif event.key == "escape":
            self.dismiss(None)


class SessionDetailScreen(ModalScreen[str]):
    """One child's steps, tool runs, notes and findings."""

    BINDINGS = [
        Binding("escape", "dismiss", "close", priority=True),
        Binding("x", "stop", "stop child", priority=True),
        Binding("r", "replay", "replay task", priority=True),
        Binding("i", "inspect", "last run", priority=True),
    ]

    CSS = """
    SessionDetailScreen {
        align: center middle;
    }
    #session-dialog {
        width: 78;
        max-width: 96%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: solid $accent-alt;
        background: $panel;
    }
    #session-title {
        text-style: bold;
        color: $accent-alt;
    }
    #session-hint {
        color: $text-dim;
        padding-bottom: 1;
    }
    #session-body {
        height: auto;
        max-height: 22;
    }
    .session-heading {
        text-style: bold;
        color: $accent;
        padding-top: 1;
    }
    .session-line {
        color: $text;
    }
    """

    def __init__(
        self,
        translator: Translator,
        title: str,
        sections: list[tuple[str, list[str]]],
    ) -> None:
        super().__init__()
        self.translator = translator
        self.heading = title
        self.sections = sections

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="session-dialog"):
            yield Static(self.heading, id="session-title")
            yield Static(self.translator.t("sessions.detail-hint"), id="session-hint")
            with VerticalScroll(id="session-body"):
                for heading, lines in self.sections:
                    yield Static(heading, classes="session-heading")
                    for line in lines or [self.translator.t("inspector.none")]:
                        yield Static(line, classes="session-line")

    def action_stop(self) -> None:
        self.dismiss("stop")

    def action_replay(self) -> None:
        self.dismiss("replay")

    def action_inspect(self) -> None:
        self.dismiss("inspect")
