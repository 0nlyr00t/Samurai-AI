"""The agent asks a question: one option list, recommended option first."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.domain.agents import OperatorQuestion


class OperatorQuestionScreen(ModalScreen[str | None]):
    """Dismisses with the chosen label, or None when the operator skipped."""

    CSS = """
    OperatorQuestionScreen {
        align: center middle;
    }
    #question-dialog {
        width: 76;
        height: auto;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #question-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #question-text {
        color: $text;
        padding-bottom: 1;
    }
    #question-options {
        height: auto;
        background: $panel;
    }
    """

    def __init__(self, header: str, question: OperatorQuestion) -> None:
        super().__init__()
        self.header = header
        self.question = question

    def compose(self) -> ComposeResult:
        ordered = sorted(self.question.options, key=lambda item: not item.recommended)
        self._order = ordered
        with Vertical(id="question-dialog"):
            yield Static(self.header or self.question.id, id="question-title")
            yield Static(self.question.question, id="question-text")
            yield OptionList(
                *[
                    Option(
                        f"{option.label} — {option.description}"
                        if option.description
                        else option.label,
                        id=f"q-{index}",
                    )
                    for index, option in enumerate(ordered)
                ],
                id="question-options",
            )

    def on_mount(self) -> None:
        options = self.query_one("#question-options", OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = int(str(event.option.id).removeprefix("q-"))
        self.dismiss(self._order[index].label)

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(None)
