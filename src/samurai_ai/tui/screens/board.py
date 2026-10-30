"""Boards: a grouped list with actions, plus one item's details.

One screen serves the plan and the findings board: the app hands in labelled
rows (group headings and items), the action keys with their callbacks, and the
detail sections, so the screens stay free of services.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator

#: ("group", heading, "") renders a heading; ("item", id, label) a row.
Row = tuple[str, str, str]


class BoardScreen(ModalScreen[tuple[str, str] | None]):
    """Grouped board: Enter opens an item, action keys act on the highlight."""

    BINDINGS = [
        Binding("escape", "dismiss", "close", priority=True),
        Binding("f", "filter", "filter", priority=True),
    ]

    CSS = """
    BoardScreen {
        align: center middle;
    }
    #board-dialog {
        width: 80;
        max-width: 96%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #board-title {
        text-style: bold;
        color: $accent;
    }
    #board-hint {
        color: $text-dim;
        padding-bottom: 1;
    }
    #board-list {
        height: auto;
        max-height: 22;
        background: $panel;
    }
    """

    def __init__(
        self,
        translator: Translator,
        title: str,
        hint: str,
        rows: Callable[[bool], list[Row]],
        actions: dict[str, Callable[[str], None]],
        action_labels: dict[str, str],
        on_open: Callable[[str], None],
        empty_text: str,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.heading = title
        self.hint = hint
        self.row_provider = rows
        self.actions = actions
        self.action_labels = action_labels
        self.on_open = on_open
        self.empty_text = empty_text
        self.show_all = False
        self._items: list[str] = []

    # -- composition ------------------------------------------------------
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="board-dialog"):
            yield Static(self.heading, id="board-title")
            yield Static(self.hint, id="board-hint")
            yield OptionList(id="board-list")

    def on_mount(self) -> None:
        self._reload()

    def _reload(self) -> None:
        listed = self.query_one("#board-list", OptionList)
        listed.clear_options()
        self._items = []
        rows = self.row_provider(self.show_all)
        if not rows:
            listed.add_option(Option(self.empty_text, id="empty", disabled=True))
            listed.highlighted = 0
            return
        for kind, item_id, label in rows:
            if kind == "group":
                listed.add_option(Option(f"── {item_id} ──", id=f"g-{item_id}", disabled=True))
                continue
            listed.add_option(Option(label, id=item_id))
            self._items.append(item_id)
        first_item = next(
            (index for index, option in enumerate(listed.options) if option.id in self._items),
            0,
        )
        listed.highlighted = first_item
        listed.focus()

    # -- selection --------------------------------------------------------
    def selected(self) -> str | None:
        listed = self.query_one("#board-list", OptionList)
        highlighted = listed.highlighted
        if highlighted is None:
            return None
        option = listed.options[highlighted]
        return option.id if option.id in self._items else None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id in self._items:
            self.on_open(str(event.option.id))

    def on_key(self, event: Any) -> None:
        key = event.key
        if key == "enter":
            item_id = self.selected()
            if item_id is not None:
                self.on_open(item_id)
            event.stop()
            return
        if key == "escape":
            self.dismiss(None)
            event.stop()
            return
        if key == "f":
            self.action_filter()
            event.stop()
            return
        action = self.actions.get(key)
        item_id = self.selected()
        if action is not None and item_id is not None:
            action(item_id)
            self._reload()
            event.stop()

    def action_filter(self) -> None:
        self.show_all = not self.show_all
        self._reload()

    async def action_dismiss(self, result: Any = None) -> None:
        self.dismiss(None)


class ItemDetailScreen(ModalScreen[str | None]):
    """Read-only details of one board item, with the same action keys."""

    CSS = """
    ItemDetailScreen {
        align: center middle;
    }
    #item-dialog {
        width: 78;
        max-width: 96%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #item-title {
        text-style: bold;
        color: $accent;
    }
    #item-hint {
        color: $text-dim;
        padding-bottom: 1;
    }
    #item-body {
        height: auto;
        max-height: 22;
    }
    .item-heading {
        text-style: bold;
        color: $accent;
        padding-top: 1;
    }
    .item-line {
        color: $text;
    }
    """

    def __init__(
        self,
        translator: Translator,
        title: str,
        hint: str,
        sections: list[tuple[str, list[str]]],
        actions: dict[str, Callable[[], None]],
    ) -> None:
        super().__init__()
        self.translator = translator
        self.heading = title
        self.hint = hint
        self.sections = sections
        self.actions = actions
        self._bindings.bind("escape", "dismiss", priority=True)
        for key in actions:
            self._bindings.bind(key, f"act('{key}')", priority=True, show=False)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="item-dialog"):
            yield Static(self.heading, id="item-title")
            yield Static(self.hint, id="item-hint")
            with VerticalScroll(id="item-body"):
                for heading, lines in self.sections:
                    yield Static(heading, classes="item-heading")
                    for line in lines or [self.translator.t("inspector.none")]:
                        yield Static(line, classes="item-line")

    def action_act(self, key: str) -> None:
        action = self.actions.get(key)
        if action is not None:
            action()

    async def action_dismiss(self, result: Any = None) -> None:
        self.dismiss(result)
