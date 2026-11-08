"""Context menu for an operation tab: close it, or close the others."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator

CLOSE = "close"
CLOSE_OTHERS = "close-others"
RENAME = "rename"


class TabMenuScreen(ModalScreen[str | None]):
    """Small menu for the tab the operator right-clicked."""

    BINDINGS = [Binding("escape", "dismiss", "close", priority=True)]

    CSS = """
    TabMenuScreen {
        align: center middle;
    }
    #tabmenu-dialog {
        width: 48;
        height: auto;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #tabmenu-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #tabmenu-list {
        height: auto;
        background: $panel;
    }
    """

    def __init__(self, translator: Translator, label: str, *, others: bool = True) -> None:
        super().__init__()
        self.translator = translator
        self.label = label
        self.others = others

    def compose(self) -> ComposeResult:
        with Vertical(id="tabmenu-dialog"):
            yield Static(self.label, id="tabmenu-title")
            options = [
                Option(self.translator.t("tabmenu.rename"), id=RENAME),
                Option(self.translator.t("tabmenu.close"), id=CLOSE),
            ]
            if self.others:
                options.append(Option(self.translator.t("tabmenu.close-others"), id=CLOSE_OTHERS))
            yield OptionList(*options, id="tabmenu-list")

    def on_mount(self) -> None:
        listed = self.query_one("#tabmenu-list", OptionList)
        listed.highlighted = 0
        listed.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)
