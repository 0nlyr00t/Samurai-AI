from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator


class LanguagePromptScreen(ModalScreen[str]):
    CSS = """
    LanguagePromptScreen {
        align: center middle;
    }
    #language-dialog {
        width: 52;
        height: auto;
        padding: 1 2;
        border: solid $accent-alt;
        background: $panel;
    }
    #language-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #language-options {
        height: auto;
        background: $panel;
    }
    """

    def __init__(self, translator: Translator) -> None:
        super().__init__()
        self.translator = translator

    def compose(self) -> ComposeResult:
        with Vertical(id="language-dialog"):
            yield Static(self.translator.t("chooser.title"), id="language-title")
            yield OptionList(
                Option(self.translator.t("chooser.english"), id="en"),
                Option(self.translator.t("chooser.russian"), id="ru"),
                id="language-options",
            )

    def on_mount(self) -> None:
        self.query_one("#language-options", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id or "en"))
