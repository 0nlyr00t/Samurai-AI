"""Startup project picker: which directory this session works in.

A harness that keeps state per folder must let the operator choose the folder
instead of inheriting whatever the shell happened to be in.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator

MANUAL_ID = "p-manual"


class ProjectPickerScreen(ModalScreen[tuple[str, str] | None]):
    """Pick a project directory, or type another one: (action, path)."""

    BINDINGS = [Binding("escape", "dismiss", "close", priority=True)]

    CSS = """
    ProjectPickerScreen {
        align: center middle;
    }
    #project-dialog {
        width: 76;
        max-width: 96%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #project-title {
        text-style: bold;
        color: $accent;
    }
    #project-hint {
        color: $text-dim;
        padding-bottom: 1;
    }
    #project-list {
        height: auto;
        max-height: 16;
        background: $panel;
    }
    #project-path {
        margin-top: 1;
        display: none;
    }
    #project-path.visible {
        display: block;
    }
    """

    def __init__(
        self, translator: Translator, projects: list[tuple[str, str]]
    ) -> None:
        super().__init__()
        self.translator = translator
        self.projects = projects

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="project-dialog"):
            yield Static(self.translator.t("project.title"), id="project-title")
            yield Static(self.translator.t("project.hint"), id="project-hint")
            listed = OptionList(id="project-list")
            for path, label in self.projects:
                listed.add_option(Option(label, id=path))
            listed.add_option(Option(self.translator.t("project.manual"), id=MANUAL_ID))
            yield listed
            yield Input(placeholder=self.translator.t("project.path"), id="project-path")

    def on_mount(self) -> None:
        listed = self.query_one("#project-list", OptionList)
        listed.highlighted = 0
        listed.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = str(event.option.id)
        if option_id == MANUAL_ID:
            field = self.query_one("#project-path", Input)
            field.add_class("visible")
            field.focus()
            return
        self.dismiss(("open", option_id))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        target = Path(value).expanduser()
        if not target.is_dir():
            self.query_one("#project-hint", Static).update(
                self.translator.t("project.missing", path=str(target))
            )
            return
        self.dismiss(("open", str(target.resolve())))

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)
