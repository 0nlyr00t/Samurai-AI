import json
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.execution import ApprovalRequiredError
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelError,
    ModelNotFoundError,
    ProviderDefinition,
    ProviderUnavailableError,
)
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.models import (
    CustomModelConflictError,
    CustomModelRepository,
    ModelEndpointRepository,
)
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.vault import VaultRepository

TEST_KEY = "ab" * 32
API_KEY = "sk-test-secret-key-123"


class _OpenAIHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/models":
            if not self._authorized():
                return self._reply(401, self._unauthorized())
            self._reply(200, {"data": [{"id": "custom-model"}, {"id": "other"}]})
        else:
            self._reply(404, {})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            return self._reply(404, {})
        if not self._authorized():
            return self._reply(401, self._unauthorized())
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length))
        content = "echo:" + request["messages"][-1]["content"]
        self._reply(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    def _unauthorized(self) -> dict:
        header = self.headers.get("Authorization") or ""
        return {"error": {"message": f"unauthorized, got header: {header}"}}

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {API_KEY}"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _OllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            self._reply(200, {"models": [{"name": "llama3.1:8b"}, {"name": "phi-2:latest"}]})
        else:
            self._reply(404, {})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/chat":
            return self._reply(404, {})
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length))
        self._reply(
            200,
            {
                "message": {
                    "role": "assistant",
                    "content": "local:" + request["messages"][-1]["content"],
                },
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def openai_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def ollama_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def vault_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    return tmp_path


def make_stack(
    database_path: Path,
) -> tuple[OperationService, KernelService, ModelService, VaultService]:
    database = Database(database_path)
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    RecoveryService(operations, kernel, RecoveryRepository(database))
    vault = VaultService(
        VaultRepository(database), operations.repository, database_path.parent / "vault.key"
    )
    endpoints = ModelEndpointRepository(database)
    custom_models = CustomModelRepository(database)
    models = ModelService(
        ModelRegistry(endpoints, custom_models=custom_models),
        vault,
        kernel,
        operations,
        endpoints,
        custom_models=custom_models,
    )
    return operations, kernel, models, vault


def make_operation(service: OperationService) -> str:
    return service.create(
        "Models lab", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    ).id


def test_custom_endpoint_register_resolve_remove(vault_env: Path) -> None:
    _, _, models, _ = make_stack(vault_env / "endpoints.db")
    endpoint = models.register_endpoint(
        "custom", "http://127.0.0.1:9999/v1", "custom-key"
    )
    assert endpoint.api_family.value == "openai-compatible"
    providers = {row["id"] for row in models.list_providers()}
    assert "custom" in providers
    assert "openai" in providers

    model, provider = models.registry.get_model("custom/anything@v1")
    assert provider.id == "custom"
    assert model.capabilities == frozenset({ModelCapability.STRUCTURED_OUTPUT})

    with pytest.raises(ModelNotFoundError):
        models.registry.get_model("other/anything@v1")

    models.remove_endpoint("custom")
    with pytest.raises(ModelNotFoundError):
        models.remove_endpoint("custom")


def test_openai_compatible_complete_flow(vault_env: Path, openai_server: str) -> None:
    service, kernel, models, vault = make_stack(vault_env / "complete.db")
    vault.add("test-key", API_KEY)
    models.register_endpoint("custom", f"{openai_server}/v1", "test-key")
    operation_id = make_operation(service)

    with pytest.raises(ApprovalRequiredError) as caught:
        models.complete(operation_id, "custom/custom-model@v1", "hello")
    kernel.approve(caught.value.approval_id, decided_by="operator")
    response = models.complete(
        operation_id,
        "custom/custom-model@v1",
        "hello",
        approval_id=caught.value.approval_id,
    )
    assert response.text == "echo:hello"
    assert response.usage.prompt_tokens == 12
    assert response.usage.completion_tokens == 5

    events = service.events(operation_id)
    calls = [event for event in events if event.event_type == "model.call"]
    assert len(calls) == 1
    payload = calls[0].payload
    assert payload["model_id"] == "custom/custom-model@v1"
    assert payload["provider_id"] == "custom"
    assert payload["local"] is False
    assert payload["prompt_tokens"] == 12
    assert payload["completion_tokens"] == 5
    assert payload["latency_ms"] >= 0
    assert payload["estimated_cost_usd"] == 0.0
    assert API_KEY not in json.dumps([event.payload for event in events])


def test_api_key_never_leaks_into_errors(vault_env: Path, openai_server: str) -> None:
    service, kernel, models, vault = make_stack(vault_env / "leak.db")
    vault.add("test-key", "wrong-key")
    models.register_endpoint("custom", f"{openai_server}/v1", "test-key")
    operation_id = make_operation(service)
    approval = kernel.request_approval(
        operation_id, "model.call custom/custom-model@v1", RiskClass.R2
    )
    kernel.approve(approval.id, decided_by="operator")
    with pytest.raises(ProviderUnavailableError) as caught:
        models.complete(
            operation_id,
            "custom/custom-model@v1",
            "hello",
            approval_id=approval.id,
        )
    assert "wrong-key" not in str(caught.value)
    assert "***REDACTED***" in str(caught.value)


def test_health_and_discovery(vault_env: Path, openai_server: str) -> None:
    _, _, models, vault = make_stack(vault_env / "health.db")
    vault.add("test-key", API_KEY)
    models.register_endpoint("custom", f"{openai_server}/v1", "test-key")

    report = models.health("custom")
    assert report["healthy"] is True
    assert report["local"] is False

    discovered = models.discover_models("custom")
    assert [model.id for model in discovered] == [
        "custom/custom-model@v1",
        "custom/other@v1",
    ]


def test_unavailable_provider_fails_clean(vault_env: Path) -> None:
    _, _, models, vault = make_stack(vault_env / "down.db")
    vault.add("test-key", API_KEY)
    models.register_endpoint("custom", "http://127.0.0.1:1/v1", "test-key")
    report = models.health("custom")
    assert report["healthy"] is False
    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        models.discover_models("custom")


def make_ollama_stack(
    database_path: Path, base_url: str
) -> tuple[OperationService, KernelService, ModelService]:
    database = Database(database_path)
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    vault = VaultService(
        VaultRepository(database), operations.repository, database_path.parent / "vault.key"
    )
    endpoints = ModelEndpointRepository(database)
    registry = ModelRegistry(
        endpoints,
        providers=(
            ProviderDefinition(
                id="ollama",
                api_family=ApiFamily.OLLAMA,
                base_url=base_url,
                local=True,
            ),
        ),
    )
    return operations, kernel, ModelService(registry, vault, kernel, operations, endpoints)


def test_ollama_discovery_and_complete(vault_env: Path, ollama_server: str) -> None:
    operations, _, models = make_ollama_stack(vault_env / "ollama.db", ollama_server)
    operation = operations.create(
        "Ollama lab", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    )

    discovered = models.discover_models("ollama")
    assert [model.id for model in discovered] == [
        "ollama/llama3.1:8b@v1",
        "ollama/phi-2:latest@v1",
    ]

    response = models.complete(operation.id, "ollama/llama3.1:8b@v1", "hi")
    assert response.text == "local:hi"
    assert response.provider_id == "ollama"
    events = operations.events(operation.id)
    calls = [event for event in events if event.event_type == "model.call"]
    assert calls[0].payload["local"] is True
    assert calls[0].payload["prompt_tokens"] == 7


def test_ollama_unavailable_is_clean(vault_env: Path) -> None:
    _, _, models = make_ollama_stack(vault_env / "ollama-down.db", "http://127.0.0.1:1")
    report = models.health("ollama")
    assert report["healthy"] is False
    with pytest.raises(ProviderUnavailableError, match="ollama serve"):
        models.discover_models("ollama")


def test_missing_api_key_is_clear_error(vault_env: Path) -> None:
    service, kernel, models, _ = make_stack(vault_env / "nokey.db")
    operation_id = make_operation(service)
    with pytest.raises(ApprovalRequiredError) as caught:
        models.complete(operation_id, "openai/gpt-4o@v1", "hi")
    kernel.approve(caught.value.approval_id, decided_by="operator")
    with pytest.raises(ModelError, match="not configured"):
        models.complete(
            operation_id,
            "openai/gpt-4o@v1",
            "hi",
            approval_id=caught.value.approval_id,
        )


def test_migration_v6_to_v7_adds_model_endpoints(vault_env: Path) -> None:
    database_path = vault_env / "migrated.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for version, script in _MIGRATIONS[:6]:
            connection.executescript(script)
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
        connection.commit()
    finally:
        connection.close()

    Database(database_path).initialize()

    connection = sqlite3.connect(database_path)
    try:
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        assert versions == set(range(1, len(_MIGRATIONS) + 1))
        connection.execute(
            """
            INSERT INTO model_endpoints(
                id, api_family, base_url, secret_name, display_name, enabled,
                created_at, created_by
            ) VALUES ('legacy', 'openai-compatible', 'http://127.0.0.1/v1',
                      NULL, 'legacy', 1, '2026-01-01T00:00:00', 'test')
            """
        )
        connection.commit()
    finally:
        connection.close()

    endpoint = ModelEndpointRepository(Database(database_path)).get("legacy")
    assert endpoint is not None
    assert endpoint.enabled is True


def test_custom_model_lifecycle(vault_env: Path) -> None:
    _, _, models, _ = make_stack(vault_env / "custom-models.db")

    added = models.add_model("moonshot/kimi-k2-0905-preview")
    # the id is not a static preset, so the custom path is what is under test
    assert added.id == "moonshot/kimi-k2-0905-preview@v1"
    assert ModelCapability.TOOLS in added.capabilities

    model, provider = models.registry.get_model("moonshot/kimi-k2-0905-preview@v1")
    assert provider.id == "moonshot"
    assert model.context_limit == 32768
    assert ModelCapability.TOOLS in model.capabilities
    assert "moonshot/kimi-k2-0905-preview@v1" in {
        entry.id for entry in models.list_models("moonshot")
    }

    with pytest.raises(CustomModelConflictError):
        models.add_model("moonshot/kimi-k2-0905-preview@v1")
    with pytest.raises(ModelError, match="already registered"):
        models.add_model("moonshot/kimi-k2@v1")
    with pytest.raises(ModelError, match="discovered at runtime"):
        models.add_model("ollama/llama3.1:8b")
    with pytest.raises(ModelNotFoundError):
        models.add_model("no-such-provider/model@v1")

    models.remove_model("moonshot/kimi-k2-0905-preview")
    with pytest.raises(ModelNotFoundError):
        models.registry.get_model("moonshot/kimi-k2-0905-preview@v1")
    with pytest.raises(ModelNotFoundError):
        models.remove_model("moonshot/kimi-k2-0905-preview")


def test_custom_model_with_slashed_name(vault_env: Path) -> None:
    _, _, models, _ = make_stack(vault_env / "slashed.db")
    models.register_endpoint(
        "giga", "http://127.0.0.1:9/osav1", None, display_name="Giga"
    )

    added = models.add_model(
        "giga/giga-osa/deepseek-v4.1-flash-uncensored",
        display_name="Giga DeepSeek",
        context_limit=64000,
    )
    assert added.id == "giga/giga-osa/deepseek-v4.1-flash-uncensored@v1"
    assert added.model_name == "giga-osa/deepseek-v4.1-flash-uncensored"
    assert added.context_limit == 64000

    model, provider = models.registry.get_model(
        "giga/giga-osa/deepseek-v4.1-flash-uncensored@v1"
    )
    assert provider.id == "giga"
    assert model.display_name == "Giga DeepSeek"
    assert ModelCapability.TOOLS in model.capabilities


def test_migration_15_applies(vault_env: Path) -> None:
    database_path = vault_env / "migration15.db"
    Database(database_path).initialize()
    connection = sqlite3.connect(database_path)
    try:
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        assert 15 in versions
        connection.execute(
            """
            INSERT INTO custom_models(
                id, provider_id, display_name, context_limit, max_output_tokens,
                capabilities, created_at, created_by
            ) VALUES ('moonshot/kimi-k3@v1', 'moonshot', 'Kimi K3', 131072, 8192,
                      'streaming,tools', '2026-01-01T00:00:00', 'test')
            """
        )
        connection.commit()
    finally:
        connection.close()

    stored = CustomModelRepository(Database(database_path)).get("moonshot/kimi-k3@v1")
    assert stored is not None
    assert stored.capabilities == frozenset(
        {ModelCapability.TOOLS, ModelCapability.STREAMING}
    )


def test_endpoint_verify_tls_roundtrip(vault_env: Path) -> None:
    _, _, models, _ = make_stack(vault_env / "tls.db")
    models.register_endpoint("strict", "https://example.com/v1", None)
    models.register_endpoint(
        "relaxed", "https://example.org/v1", None, verify_tls=False
    )
    rows = {row["id"]: row for row in models.list_providers()}
    assert rows["strict"]["verify_tls"] is True
    assert rows["relaxed"]["verify_tls"] is False
    assert models.registry.get_provider("relaxed").verify_tls is False


def test_migration_16_defaults_verify_tls_on(vault_env: Path) -> None:
    database_path = vault_env / "migration16.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for version, script in _MIGRATIONS[:15]:
            connection.executescript(script)
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
        connection.execute(
            """
            INSERT INTO model_endpoints(
                id, api_family, base_url, secret_name, display_name, enabled,
                created_at, created_by
            ) VALUES ('legacy-tls', 'openai-compatible', 'https://legacy.test/v1',
                      NULL, 'legacy-tls', 1, '2026-01-01T00:00:00', 'test')
            """
        )
        connection.commit()
    finally:
        connection.close()

    Database(database_path).initialize()

    endpoint = ModelEndpointRepository(Database(database_path)).get("legacy-tls")
    assert endpoint is not None
    assert endpoint.verify_tls is True
