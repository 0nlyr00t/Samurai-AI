"""Switch, rename or delete any operation — the full list, not just open tabs.

A keyboard-driven picker in the spirit of opencode's session list: on startup
only the latest operation opens as a tab, so earlier ones were unreachable
without the mouse. This lists every operation in the store and works entirely
from the keyboard (right-click menus are unreliable across terminals).
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator


class OperationsScreen(ModalScreen["tuple[str, str] | None"]):
    """All operations; Enter switches, d deletes (from the DB), r renames.

    Dismisses with ``(action, operation_id)`` where action is ``"open"``,
    ``"delete"`` or ``"rename"``; Esc dismisses ``None``.
    """

    BINDINGS = [
        Binding("escape", "close", "close", priority=True),
        Binding("d,delete", "delete", "delete"),
        Binding("r", "rename", "rename"),
    ]

    CSS = """
    OperationsScreen {
        align: center middle;
    }
    #ops-dialog {
        width: 76;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: solid $accent;
        background: $panel;
    }
    #ops-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #ops-list {
        height: auto;
        max-height: 20;
        background: $panel;
    }
    #ops-hint {
        color: $text-dim;
        padding-top: 1;
    }
    """

    def __init__(
        self,
        translator: Translator,
        rows: list[dict[str, Any]],
        current: str | None = None,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.rows = rows
        self.current = current

    def _mark(self, row: dict[str, Any]) -> str:
        if row["id"] == self.current:
            return "●"
        if row.get("busy"):
            return "▶"
        return "○" if row.get("open") else "·"

    def compose(self) -> ComposeResult:
        options = [
            Option(f" {self._mark(row)} {row['label']}", id=f"op{index}")
            for index, row in enumerate(self.rows)
        ]
        with Vertical(id="ops-dialog"):
            yield Static(self.translator.t("operations.title"), id="ops-title")
            yield OptionList(*options, id="ops-list")
            yield Static(self.translator.t("operations.hint"), id="ops-hint")

    def on_mount(self) -> None:
        listed = self.query_one("#ops-list", OptionList)
        index = next(
            (i for i, row in enumerate(self.rows) if row["id"] == self.current), 0
        )
        if self.rows:
            listed.highlighted = index
        listed.focus()

    def _selected_id(self) -> str | None:
        listed = self.query_one("#ops-list", OptionList)
        index = listed.highlighted
        if index is None or not 0 <= index < len(self.rows):
            return None
        return str(self.rows[index]["id"])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        operation_id = self._selected_id()
        if operation_id is not None:
            self.dismiss(("open", operation_id))

    def action_delete(self) -> None:
        operation_id = self._selected_id()
        if operation_id is not None:
            self.dismiss(("delete", operation_id))

    def action_rename(self) -> None:
        operation_id = self._selected_id()
        if operation_id is not None:
            self.dismiss(("rename", operation_id))

    def action_close(self) -> None:
        self.dismiss(None)
