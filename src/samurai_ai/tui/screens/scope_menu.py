"""Context menu for one scope rule: toggle its exclusion, remove or edit it."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator

TOGGLE = "toggle"
REMOVE = "remove"
EDIT = "edit"


class ScopeMenuScreen(ModalScreen[str | None]):
    """Small menu for the scope rule the operator right-clicked.

    The first entry toggles the rule between included and excluded (its label
    follows the rule's current state); the others remove it outright or open an
    editor. Every choice applies to the operation immediately.
    """

    BINDINGS = [Binding("escape", "dismiss", "close", priority=True)]

    CSS = """
    ScopeMenuScreen {
        align: center middle;
    }
    #scopemenu-dialog {
        width: 52;
        height: auto;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #scopemenu-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #scopemenu-list {
        height: auto;
        background: $panel;
    }
    """

    def __init__(self, translator: Translator, rule: str, *, excluded: bool) -> None:
        super().__init__()
        self.translator = translator
        self.rule = rule
        self.excluded = excluded

    def compose(self) -> ComposeResult:
        with Vertical(id="scopemenu-dialog"):
            yield Static(self.rule, id="scopemenu-title")
            toggle_key = "scope.menu-restore" if self.excluded else "scope.menu-exclude"
            yield OptionList(
                Option(self.translator.t(toggle_key), id=TOGGLE),
                Option(self.translator.t("scope.menu-edit"), id=EDIT),
                Option(self.translator.t("scope.menu-remove"), id=REMOVE),
                id="scopemenu-list",
            )

    def on_mount(self) -> None:
        listed = self.query_one("#scopemenu-list", OptionList)
        listed.highlighted = 0
        listed.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)
