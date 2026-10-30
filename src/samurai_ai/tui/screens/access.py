from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator


class AccessPromptScreen(ModalScreen[bool]):
    """Allow/deny prompt for a governed action (e.g. a remote model call)."""

    CSS = """
    AccessPromptScreen {
        align: center middle;
    }
    #access-dialog {
        width: 72;
        height: auto;
        padding: 1 2;
        border: solid #ffd43b;
        background: #0a0a0a;
    }
    #access-title {
        text-style: bold;
        color: #ffd43b;
        padding-bottom: 1;
    }
    #access-action {
        color: #e0e0e0;
        padding-bottom: 1;
    }
    #access-options {
        height: auto;
        background: #0a0a0a;
    }
    """

    def __init__(self, translator: Translator, action: str, risk: str) -> None:
        super().__init__()
        self.translator = translator
        self.action = action
        self.risk = risk

    def compose(self) -> ComposeResult:
        with Vertical(id="access-dialog"):
            yield Static(self.translator.t("access.prompt-title"), id="access-title")
            yield Static(f"{self.action}  |  risk={self.risk}", id="access-action")
            yield OptionList(
                Option(self.translator.t("access.allow"), id="a-allow"),
                Option(self.translator.t("access.deny"), id="a-deny"),
                id="access-options",
            )

    def on_mount(self) -> None:
        options = self.query_one("#access-options", OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss(event.option is not None and event.option.id == "a-allow")

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(False)
