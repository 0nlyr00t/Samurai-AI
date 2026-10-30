from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator


class TrustDirectoryScreen(ModalScreen[str | None]):
    """First-run consent dialog for working in the current directory."""

    CSS = """
    TrustDirectoryScreen {
        align: center middle;
    }
    #trust-dialog {
        width: 72;
        height: auto;
        padding: 1 2;
        border: solid $accent-alt;
        background: $panel;
    }
    #trust-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #trust-path {
        color: $warning;
        padding-bottom: 1;
    }
    #trust-options {
        height: auto;
        background: $panel;
    }
    """

    def __init__(self, translator: Translator, path: str) -> None:
        super().__init__()
        self.translator = translator
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical(id="trust-dialog"):
            yield Static(self.translator.t("trust.title"), id="trust-title")
            yield Static(self.path, id="trust-path")
            yield OptionList(
                Option(self.translator.t("trust.always"), id="t-always"),
                Option(self.translator.t("trust.session"), id="t-session"),
                Option(self.translator.t("trust.deny"), id="t-deny"),
                id="trust-options",
            )

    def on_mount(self) -> None:
        options = self.query_one("#trust-options", OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id or "t-deny")[2:])

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(None)
