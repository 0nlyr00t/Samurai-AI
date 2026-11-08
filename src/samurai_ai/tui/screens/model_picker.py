from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator

REASONING_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")

ADD_CUSTOM_ID = "__add_custom__"


class ModelPickerScreen(ModalScreen[tuple[str, str]]):
    """Arrow-key model and reasoning picker, in the spirit of opencode."""

    CSS = """
    ModelPickerScreen {
        align: center middle;
    }
    #model-dialog {
        width: 84;
        height: auto;
        padding: 1 2;
        border: solid $accent-alt;
        background: $panel;
    }
    #model-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #model-columns {
        height: 14;
    }
    .picker-column {
        width: 1fr;
        height: 100%;
    }
    .picker-label {
        color: $text-dim;
        padding-bottom: 1;
    }
    #model-hint {
        color: $text-dim;
        padding-top: 1;
    }
    """

    def __init__(
        self,
        translator: Translator,
        model_ids: list[str],
        current_model: str | None,
        current_reasoning: str,
        add_custom_label: str | None = None,
        current_marker: str = "",
        reasoning_by_model: dict[str, str] | None = None,
        needs_key: list[tuple[str, str]] | None = None,
        needs_key_label: str = "",
    ) -> None:
        super().__init__()
        self.translator = translator
        self.model_ids = model_ids
        self.add_custom_label = add_custom_label
        self.current_marker = current_marker
        self.reasoning_by_model = reasoning_by_model or {}
        self.current_model = current_model
        self.current_reasoning = current_reasoning
        self.needs_key = needs_key or []
        self.needs_key_label = needs_key_label
        self.selected_model = current_model or (model_ids[0] if model_ids else ADD_CUSTOM_ID)
        self.selected_reasoning = current_reasoning

    def compose(self) -> ComposeResult:
        model_options: list[Option] = []
        if self.add_custom_label is not None:
            model_options.append(Option(self.add_custom_label, id="m-add"))
        for index, model_id in enumerate(self.model_ids):
            label = model_id
            if self.current_marker and model_id == self.current_model:
                label = f"{model_id}{self.current_marker}"
            model_options.append(Option(label, id=f"m{index}"))
        for provider_id, provider_label in self.needs_key:
            # a provider without a key stays reachable, below the models
            model_options.append(
                Option(f"⚠ {provider_label} — {self.needs_key_label}", id=f"k-{provider_id}")
            )
        with Vertical(id="model-dialog"):
            yield Static(self.translator.t("model.picker-title"), id="model-title")
            with Horizontal(id="model-columns"):
                with Vertical(classes="picker-column"):
                    yield Static(self.translator.t("model.picker-models"), classes="picker-label")
                    yield OptionList(*model_options, id="model-options")
                with Vertical(classes="picker-column"):
                    yield Static(
                        self.translator.t("model.picker-reasoning"),
                        classes="picker-label",
                    )
                    yield OptionList(
                        *[Option(level, id=f"r-{level}") for level in REASONING_LEVELS],
                        id="reasoning-options",
                    )
            yield Static(self.translator.t("model.picker-hint"), id="model-hint")

    def on_mount(self) -> None:
        models = self.query_one("#model-options", OptionList)
        if self.current_model in self.model_ids:
            offset = 1 if self.add_custom_label is not None else 0
            models.highlighted = self.model_ids.index(self.current_model) + offset
        reasoning = self.query_one("#reasoning-options", OptionList)
        reasoning.highlighted = REASONING_LEVELS.index(self.current_reasoning)
        models.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "model-options" and event.option is not None:
            index = event.option_list.highlighted
            if index is None:
                return
            if self.add_custom_label is not None:
                if index == 0:
                    self.selected_model = ADD_CUSTOM_ID
                    return
                index -= 1
            if index >= len(self.model_ids):
                return  # a key row: the model choice is unchanged
            if 0 <= index < len(self.model_ids):
                model_id = self.model_ids[index]
                changed = model_id != self.selected_model
                self.selected_model = model_id
                # Follow the model's own stored reasoning level unless the
                # operator is actively choosing reasoning right now.
                reasoning_list = self.query_one("#reasoning-options", OptionList)
                stored = self.reasoning_by_model.get(model_id)
                if changed and stored in REASONING_LEVELS and not reasoning_list.has_focus:
                    self.selected_reasoning = stored
                    reasoning_list.highlighted = REASONING_LEVELS.index(stored)
        elif event.option_list.id == "reasoning-options" and event.option is not None:
            index = event.option_list.highlighted
            if index is not None:
                self.selected_reasoning = REASONING_LEVELS[index]

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = str(event.option.id) if event.option is not None else ""
        if event.option_list.id == "model-options" and option_id.startswith("k-"):
            # the caller sets the key, then reopens the picker
            self.dismiss((f"key:{option_id[2:]}", self.selected_reasoning))
            return
        if event.option_list.id == "model-options" and option_id == "m-add":
            self.dismiss((ADD_CUSTOM_ID, self.selected_reasoning))
            return
        self.dismiss((self.selected_model, self.selected_reasoning))

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(None)
