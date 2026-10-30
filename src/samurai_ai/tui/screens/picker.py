"""A flat list of choices: themes, and the actions on the key map.

Small on purpose — the caller supplies labels, descriptions and what happens on
``Enter``, so the same screen serves both pickers.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator

#: (id, label, description) — the description is empty when there is nothing to add.
Pick = tuple[str, str, str]


class PickerScreen(ModalScreen[str | None]):
    """Choose one row; ``Esc`` closes without a choice."""

    BINDINGS = [
        Binding("escape", "dismiss", "close", priority=True),
        Binding("q", "dismiss", "close", priority=True),
    ]

    CSS = """
    PickerScreen {
        align: center middle;
    }
    #picker-dialog {
        width: 72;
        max-width: 96%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #picker-title {
        text-style: bold;
        color: $accent;
    }
    #picker-hint {
        color: $text-dim;
        padding-bottom: 1;
    }
    #picker-list {
        height: auto;
        max-height: 20;
        background: $panel;
    }
    #picker-empty {
        color: $text-dim;
    }
    """

    def __init__(
        self,
        translator: Translator,
        title_key: str,
        hint_key: str,
        empty_key: str,
        picks: list[Pick],
    ) -> None:
        super().__init__()
        self.translator = translator
        self.title_key = title_key
        self.hint_key = hint_key
        self.empty_key = empty_key
        self.picks = picks

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="picker-dialog"):
            yield Static(self.translator.t(self.title_key), id="picker-title")
            yield Static(self.translator.t(self.hint_key), id="picker-hint")
            if self.picks:
                listed = OptionList(id="picker-list")
                for pick_id, label, description in self.picks:
                    prompt = label if not description else f"{label}  {description}"
                    listed.add_option(Option(prompt, id=pick_id))
                yield listed
            else:
                yield Static(self.translator.t(self.empty_key), id="picker-empty")

    def on_mount(self) -> None:
        if not self.picks:
            return
        listed = self.query_one("#picker-list", OptionList)
        listed.highlighted = 0
        listed.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id) if event.option.id else None)

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)
