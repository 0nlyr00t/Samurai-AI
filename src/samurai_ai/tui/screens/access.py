from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
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
        border: solid $warning;
        background: $panel;
    }
    #access-title {
        text-style: bold;
        color: $warning;
        padding-bottom: 1;
    }
    #access-action {
        color: $text;
        padding-bottom: 1;
    }
    #access-options {
        height: auto;
        background: $panel;
    }
    #access-full {
        height: auto;
        max-height: 12;
        border: solid $border;
        background: $surface;
        padding: 0 1;
        display: none;
    }
    #access-hint {
        color: $text-dim;
        padding-top: 1;
    }
    """

    def __init__(
        self,
        translator: Translator,
        action: str,
        risk: str,
        *,
        batch_items: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.action = action
        self.risk = risk
        self.batch_items = batch_items or []

    def compose(self) -> ComposeResult:
        with Vertical(id="access-dialog"):
            yield Static(self.translator.t("access.prompt-title"), id="access-title")
            if self.batch_items:
                yield Static(
                    self.translator.t("access.batch-hint", count=len(self.batch_items)),
                    id="access-batch",
                )
            yield Static(f"{self.action}  |  risk={self.risk}", id="access-action")
            yield OptionList(
                Option(self.translator.t("access.allow"), id="a-allow"),
                Option(self.translator.t("access.deny"), id="a-deny"),
                id="access-options",
            )
            yield VerticalScroll(Static(self.action, id="access-full-text"), id="access-full")
            yield Static(self.translator.t("access.full-hint"), id="access-hint")

    def on_mount(self) -> None:
        options = self.query_one("#access-options", OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option is not None and event.option.id == "a-allow")

    def reveal(self, shown: bool) -> None:
        """Show or hide the full command; long lines are cut in the one-liner."""
        self.query_one("#access-full", VerticalScroll).display = shown
        self.query_one(
            "#access-hint", Static
        ).update(
            self.translator.t("access.full-hint-shown" if shown else "access.full-hint")
        )
        if shown:
            self.query_one("#access-full-text", Static).update(self.action)

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "a" and self.batch_items:
            # approve the whole batch: same risk, same call shape
            event.stop()
            self.dismiss(True)
            return
        if event.key == "v":
            event.stop()
            self.reveal(not self.query_one("#access-full", VerticalScroll).display)
            return
        if event.key == "c":
            event.stop()
            self.app.copy_to_clipboard(self.action)
            self.query_one("#access-hint", Static).update(self.translator.t("access.copied"))
            return
        if event.key == "escape":
            self.dismiss(False)
        elif event.key == "enter":
            # Enter is handled here, not by OptionList: its highlight is not set
            # yet on the first frame, which made the very first Enter a no-op.
            options = self.query_one("#access-options", OptionList)
            self.dismiss(options.highlighted in (None, 0))
