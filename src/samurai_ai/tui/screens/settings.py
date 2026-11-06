"""Settings screen: tool provider keys, set once and injected by name.

The list is a catalogue, not free text: each row names the variable a tool reads,
where to obtain it and whether a value is stored. The value itself never comes
back on screen — only `set` or `empty`.
"""

from __future__ import annotations

from collections.abc import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator
from samurai_ai.services.tool_keys import CATALOGUE, ToolKey


class SettingsScreen(ModalScreen[None]):
    """Tool keys, with an inline masked field for the selected one."""

    BINDINGS = [
        Binding("escape", "dismiss", "close", priority=True),
        Binding("q", "dismiss", "close", show=False),
        Binding("enter", "edit", "set key"),
        Binding("d", "clear", "clear key"),
    ]

    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-dialog {
        width: 96;
        max-width: 96%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: solid $accent-alt;
        background: $panel;
    }
    #settings-title {
        text-style: bold;
        color: $accent-alt;
    }
    #settings-hint {
        color: $text-dim;
        padding-bottom: 1;
    }
    #settings-keys {
        height: auto;
        max-height: 18;
        background: $panel;
    }
    #settings-input {
        display: none;
        margin-top: 1;
    }
    #settings-input.visible {
        display: block;
    }
    #settings-models {
        color: $text-dim;
        padding-top: 1;
    }
    """

    _LIMITS_ID = "__limits__"

    def __init__(
        self,
        translator: Translator,
        states: dict[str, str],
        on_save: Callable[[str, str], None],
        on_clear: Callable[[str], None],
        *,
        max_tool_rounds: int = 0,
        on_save_limit: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.states = states
        self.on_save = on_save
        self.on_clear = on_clear
        self.max_tool_rounds = max_tool_rounds
        self.on_save_limit = on_save_limit
        self.rows: list[ToolKey] = list(CATALOGUE)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="settings-dialog"):
            yield Static(self.translator.t("settings.title"), id="settings-title")
            yield Static(self.translator.t("settings.hint"), id="settings-hint")
            yield OptionList(*self._options(), id="settings-keys")
            yield Input(
                id="settings-input",
                password=True,
                placeholder=self.translator.t("settings.placeholder"),
            )
            yield Static(self.translator.t("settings.models"), id="settings-models")

    def _options(self) -> list[Option]:
        options: list[Option] = []
        for entry in self.rows:
            state = self.states.get(entry.env_name, "empty")
            mark = self.translator.t(
                "settings.set" if state == "set" else "settings.empty"
            )
            options.append(
                Option(
                    f"{mark}  {entry.label} · {entry.tool} · {entry.env_name}",
                    id=entry.env_name,
                )
            )
        if self.on_save_limit is not None:
            options.append(
                Option(
                    self.translator.t(
                        "settings.rounds-row", value=self.max_tool_rounds or "—"
                    ),
                    id=self._LIMITS_ID,
                )
            )
        return options

    def on_mount(self) -> None:
        listed = self.query_one("#settings-keys", OptionList)
        if self.rows:
            listed.highlighted = 0
            listed.focus()

    def selected(self) -> ToolKey | None:
        index = self.query_one("#settings-keys", OptionList).highlighted
        if index is None or index >= len(self.rows):
            return None
        return self.rows[index]

    def _selected_id(self) -> str | None:
        listed = self.query_one("#settings-keys", OptionList)
        index = listed.highlighted
        if index is None:
            return None
        if index >= len(self.rows):
            return self._LIMITS_ID if self.on_save_limit is not None else None
        return self.rows[index].env_name

    def action_edit(self) -> None:
        selected_id = self._selected_id()
        if selected_id is None:
            return
        field = self.query_one("#settings-input", Input)
        field.password = selected_id != self._LIMITS_ID
        field.placeholder = self.translator.t(
            "settings.rounds-placeholder"
            if selected_id == self._LIMITS_ID
            else "settings.placeholder"
        )
        field.add_class("visible")
        field.value = ""
        field.focus()

    def action_clear(self) -> None:
        entry = self.selected()
        if entry is None:
            return
        self.on_clear(entry.env_name)
        self.states[entry.env_name] = "empty"
        self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        selected_id = self._selected_id()
        value = event.value.strip()
        if selected_id is None or not value:
            return
        if selected_id == self._LIMITS_ID:
            if self.on_save_limit is not None:
                self.on_save_limit(value)
            event.input.value = ""
            event.input.remove_class("visible")
            self._refresh()
            self.query_one("#settings-keys", OptionList).focus()
            return
        entry = self.selected()
        if entry is None:
            return
        self.on_save(entry.env_name, value)
        self.states[entry.env_name] = "set"
        event.input.value = ""
        event.input.remove_class("visible")
        self._refresh()
        self.query_one("#settings-keys", OptionList).focus()

    def _refresh(self) -> None:
        listed = self.query_one("#settings-keys", OptionList)
        highlighted = listed.highlighted
        listed.clear_options()
        listed.add_options(self._options())
        listed.highlighted = highlighted or 0
