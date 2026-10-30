import asyncio

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator
from samurai_ai.services.models import ModelService
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.models import CustomModelConflictError


class CustomEndpointScreen(ModalScreen[str | None]):
    """Interactive wizard for adding a custom OpenAI-compatible endpoint.

    Endpoint id, base URL, API key (stored encrypted in the vault), a TLS
    verification toggle, then live model discovery with arrow-key selection
    or manual entry. Dismisses with the chosen default model id (or None).
    """

    CSS = """
    CustomEndpointScreen {
        align: center middle;
    }
    #ce-dialog {
        width: 78;
        height: auto;
        padding: 1 2;
        border: solid #5bc0be;
        background: #0a0a0a;
    }
    #ce-title {
        text-style: bold;
        color: #4fa8ff;
        padding-bottom: 1;
    }
    .ce-label {
        color: #888888;
        padding-top: 1;
    }
    #ce-tls {
        height: auto;
        background: #0a0a0a;
    }
    #ce-fetch {
        margin-top: 1;
    }
    #ce-status {
        color: #ffd43b;
        padding-top: 1;
    }
    #ce-models {
        height: 8;
        background: #0a0a0a;
        display: none;
    }
    #ce-manual {
        display: none;
    }
    """

    def __init__(
        self,
        translator: Translator,
        models: ModelService,
        vault: VaultService,
        settings: SettingsService,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.models = models
        self.vault = vault
        self.settings = settings
        self._endpoint_id: str | None = None
        self._model_names: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="ce-dialog"):
            yield Static(self.translator.t("model.custom-title"), id="ce-title")
            yield Input(placeholder=self.translator.t("model.custom-id"), id="ce-id")
            yield Input(placeholder=self.translator.t("model.custom-url"), id="ce-url")
            yield Input(
                placeholder=self.translator.t("model.custom-key"),
                id="ce-key",
                password=True,
            )
            yield Static(self.translator.t("model.custom-tls"), classes="ce-label")
            yield OptionList(
                Option(self.translator.t("model.custom-tls-on"), id="tls-on"),
                Option(self.translator.t("model.custom-tls-off"), id="tls-off"),
                id="ce-tls",
            )
            yield Button(self.translator.t("model.custom-fetch"), id="ce-fetch")
            yield Static("", id="ce-status")
            yield OptionList(id="ce-models")
            yield Input(
                placeholder=self.translator.t("model.custom-manual"), id="ce-manual"
            )

    def on_mount(self) -> None:
        self.query_one("#ce-tls", OptionList).highlighted = 0
        self.query_one("#ce-id", Input).focus()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ce-manual":
            self._finalize(event.value.strip())
        elif event.input.id in ("ce-id", "ce-url", "ce-key"):
            self._connect()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ce-fetch":
            self._connect()

    def _connect(self) -> None:
        status = self.query_one("#ce-status", Static)
        endpoint_id = self.query_one("#ce-id", Input).value.strip()
        base_url = self.query_one("#ce-url", Input).value.strip()
        api_key = self.query_one("#ce-key", Input).value.strip()
        tls = self.query_one("#ce-tls", OptionList)
        verify_tls = tls.highlighted in (None, 0)
        if not endpoint_id or not base_url:
            status.update(self.translator.t("model.custom-invalid"))
            return
        if self.models.endpoints is None:
            status.update(
                self.translator.t(
                    "model.custom-failed", error="endpoint persistence not configured"
                )
            )
            return
        if any(
            provider.id == endpoint_id
            for provider in self.models.registry.static_providers
        ):
            status.update(
                self.translator.t(
                    "model.custom-failed", error=f"id {endpoint_id!r} is reserved"
                )
            )
            return
        try:
            if self.models.endpoints.get(endpoint_id) is not None:
                self.models.remove_endpoint(endpoint_id)
            secret_name = None
            if api_key:
                secret_name = f"{endpoint_id}-api-key"
                existing = {entry.name for entry in self.vault.list_metadata()}
                if secret_name in existing:
                    self.vault.remove(secret_name)
                self.vault.add(secret_name, api_key)
            self.models.register_endpoint(
                endpoint_id, base_url, secret_name, verify_tls=verify_tls
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            status.update(self.translator.t("model.custom-failed", error=str(exc)))
            return
        self._endpoint_id = endpoint_id
        self.query_one("#ce-manual", Input).display = True
        self.run_worker(self._discover(endpoint_id), exclusive=True)

    async def _discover(self, endpoint_id: str) -> None:
        status = self.query_one("#ce-status", Static)
        button = self.query_one("#ce-fetch", Button)
        button.disabled = True
        status.update(self.translator.t("model.custom-fetching"))
        try:
            discovered = await asyncio.to_thread(
                self.models.discover_models, endpoint_id
            )
            self._model_names = [entry.model_name for entry in discovered]
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            self._model_names = []
            status.update(self.translator.t("model.custom-failed", error=str(exc)))
        finally:
            button.disabled = False
        options = self.query_one("#ce-models", OptionList)
        options.clear_options()
        if self._model_names:
            options.add_options(
                Option(name, id=f"c{index}")
                for index, name in enumerate(self._model_names)
            )
            options.display = True
            options.highlighted = 0
            status.update(self.translator.t("model.custom-pick"))
            options.focus()
        else:
            options.display = False
            if self._endpoint_id is not None:
                status.update(self.translator.t("model.custom-no-models"))

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option_list.id != "ce-models" or event.option is None:
            return
        index = int(str(event.option.id or "c0")[1:])
        if 0 <= index < len(self._model_names):
            self._finalize(self._model_names[index])

    def _finalize(self, model_name: str) -> None:
        if not model_name or self._endpoint_id is None:
            return
        status = self.query_one("#ce-status", Static)
        try:
            model = self.models.add_model(f"{self._endpoint_id}/{model_name}")
        except CustomModelConflictError:
            model, _ = self.models.resolve_model(
                f"{self._endpoint_id}/{model_name}@v1"
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            status.update(self.translator.t("model.custom-failed", error=str(exc)))
            return
        self.settings.set_value("model.default", model.id)
        self.dismiss(model.id)
