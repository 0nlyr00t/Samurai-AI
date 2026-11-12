"""Provider-first connect flow, in the spirit of opencode's `auth login`.

Step one of choosing a model: pick a provider (or the custom OpenAI-compatible
"Other" entry). The caller then authenticates the provider if needed and opens
the model picker scoped to it. This keeps connecting a provider and picking its
model as two clear steps instead of one flat list.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator

#: the "Other — custom OpenAI-compatible endpoint" sentinel
CUSTOM_ID = "__custom__"


class ConnectScreen(ModalScreen[tuple[str, str]]):
    """Arrow-key provider picker; dismisses with (action, provider_id).

    action is one of: ``"provider"`` (a connected provider — open its models),
    ``"key"`` (a provider missing its key — set it, then reopen), or
    ``"custom"`` (add a custom OpenAI-compatible endpoint). Esc dismisses None.
    """

    CSS = """
    ConnectScreen {
        align: center middle;
    }
    #connect-dialog {
        width: 84;
        height: auto;
        padding: 1 2;
        border: solid $accent-alt;
        background: $panel;
    }
    #connect-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #connect-providers {
        height: 16;
    }
    #connect-hint {
        color: $text-dim;
        padding-top: 1;
    }
    """

    def __init__(
        self,
        translator: Translator,
        providers: list[dict[str, Any]],
        current_provider: str | None = None,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.providers = providers
        self.current_provider = current_provider

    def _row_label(self, provider: dict[str, Any]) -> str:
        t = self.translator.t
        pid = str(provider["id"])
        family = str(provider.get("api_family", ""))
        auth = provider.get("auth")
        if provider.get("local"):
            status = t("connect.status-local")
        elif auth in ("configured", "none"):
            status = t("connect.status-connected")
        else:
            status = t("connect.status-needs-key")
        mark = t("connect.current-marker") if pid == self.current_provider else ""
        return f"{pid}{mark}  ·  {family}  ·  {status}"

    def compose(self) -> ComposeResult:
        options: list[Option] = []
        for index, provider in enumerate(self.providers):
            options.append(Option(self._row_label(provider), id=f"p{index}"))
        options.append(Option(self.translator.t("connect.add-custom"), id="p-custom"))
        with Vertical(id="connect-dialog"):
            yield Static(self.translator.t("connect.title"), id="connect-title")
            yield OptionList(*options, id="connect-providers")
            yield Static(self.translator.t("connect.hint"), id="connect-hint")

    def on_mount(self) -> None:
        options = self.query_one("#connect-providers", OptionList)
        if self.current_provider is not None:
            for index, provider in enumerate(self.providers):
                if provider["id"] == self.current_provider:
                    options.highlighted = index
                    break
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = str(event.option.id) if event.option is not None else ""
        if option_id == "p-custom":
            self.dismiss(("custom", ""))
            return
        if not option_id.startswith("p"):
            return
        index = int(option_id[1:])
        if not 0 <= index < len(self.providers):
            return
        provider = self.providers[index]
        if not provider.get("local") and provider.get("auth") == "missing":
            self.dismiss(("key", str(provider["id"])))
            return
        self.dismiss(("provider", str(provider["id"])))

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(None)
