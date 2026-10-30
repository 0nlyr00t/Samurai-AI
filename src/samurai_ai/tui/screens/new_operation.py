from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator


class NewOperationScreen(ModalScreen[tuple[str, str, str] | None]):
    """Dialog for the '+' button: name + mode, creates a fresh operation tab."""

    CSS = """
    NewOperationScreen {
        align: center middle;
    }
    #newop-dialog {
        width: 56;
        height: auto;
        padding: 1 2;
        border: solid #5bc0be;
        background: #0a0a0a;
    }
    #newop-title {
        text-style: bold;
        color: #4fa8ff;
        padding-bottom: 1;
    }
    #newop-modes {
        height: auto;
        background: #0a0a0a;
        margin-top: 1;
    }
    """

    def __init__(self, translator: Translator) -> None:
        super().__init__()
        self.translator = translator

    def compose(self) -> ComposeResult:
        with Vertical(id="newop-dialog"):
            yield Static(self.translator.t("newop.title"), id="newop-title")
            yield Input(
                placeholder=self.translator.t("newop.name"), id="newop-name"
            )
            yield Input(
                placeholder=self.translator.t("newop.scope"), id="newop-scope"
            )
            yield OptionList(
                Option("bug-bounty", id="bug-bounty"),
                Option("red-team", id="red-team"),
                Option("ctf-htb", id="ctf-htb"),
                Option("operation-plan", id="operation-plan"),
                id="newop-modes",
            )

    def on_mount(self) -> None:
        modes = self.query_one("#newop-modes", OptionList)
        modes.highlighted = 0
        self.query_one("#newop-name", Input).focus()

    def _submit(self, mode: str) -> None:
        name = self.query_one("#newop-name", Input).value.strip()
        if not name:
            self.query_one("#newop-name", Input).focus()
            return
        scope = self.query_one("#newop-scope", Input).value.strip()
        self.dismiss((name, mode, scope))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "newop-name":
            self.query_one("#newop-scope", Input).focus()
        elif event.input.id == "newop-scope":
            self.query_one("#newop-modes", OptionList).focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option_list.id == "newop-modes" and event.option is not None:
            self._submit(str(event.option.id or "bug-bounty"))

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(None)
