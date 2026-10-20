import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.agents.budgets import BudgetExceededError, ModelBudget
from samurai_ai.agents.provider import ChatResponse
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.execution import ApprovalRequiredError
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.models import ApiFamily, ProviderDefinition
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.models import ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.vault import VaultRepository

TEST_KEY = "ab" * 32
ANTHROPIC_KEY = "sk-ant-test-1"
GEMINI_KEY = "goog-test-1"
OPENAI_KEY = "sk-openai-test-1"

_ANTHROPIC_MODEL = "anthropic/claude-sonnet-4-20250514@v1"
_GEMINI_MODEL = "gemini/gemini-2.5-flash@v1"
_OPENAI_MODEL = "openai/gpt-4o-mini@v1"


class _Base(BaseHTTPRequestHandler):
    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length))


class _AnthropicHandler(_Base):
    fail_with_500 = False

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("x-api-key") != ANTHROPIC_KEY:
            return self._reply(401, {"error": {"message": "invalid x-api-key"}})
        if self.headers.get("anthropic-version") != "2023-06-01":
            return self._reply(400, {"error": {"message": "bad version"}})
        if self.fail_with_500:
            return self._reply(500, {"error": {"message": "internal"}})
        request = self._read_json()
        content = "anthropic:" + request["messages"][-1]["content"]
        self._reply(
            200,
            {
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 8, "output_tokens": 4},
            },
        )


class _GeminiHandler(_Base):
    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("x-goog-api-key") != GEMINI_KEY:
            return self._reply(403, {"error": {"message": "key invalid"}})
        if ":generateContent" not in self.path:
            return self._reply(404, {})
        request = self._read_json()
        content = "gemini:" + request["contents"][-1]["parts"][-1]["text"]
        self._reply(
            200,
            {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": content}]}}
                ],
                "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2},
            },
        )


class _OpenAIHandler(_Base):
    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {OPENAI_KEY}":
            return self._reply(401, {"error": {"message": "bad key"}})
        request = self._read_json()
        content = "openai:" + request["messages"][-1]["content"]
        self._reply(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )


@pytest.fixture
def servers() -> Iterator[dict[str, str]]:
    instances: list[HTTPServer] = []
    urls: dict[str, str] = {}
    for name, handler in (
        ("anthropic", _AnthropicHandler),
        ("gemini", _GeminiHandler),
        ("openai", _OpenAIHandler),
    ):
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        instances.append(server)
        urls[name] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield urls
    finally:
        for server in instances:
            server.shutdown()
            server.server_close()


@pytest.fixture
def vault_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    return tmp_path


def make_stack(
    database_path: Path, servers: dict[str, str]
) -> tuple[OperationService, KernelService, ModelService, VaultService]:
    database = Database(database_path)
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    vault = VaultService(
        VaultRepository(database), operations.repository, database_path.parent / "vault.key"
    )
    endpoints = ModelEndpointRepository(database)
    registry = ModelRegistry(
        endpoints,
        providers=(
            ProviderDefinition(
                id="anthropic",
                api_family=ApiFamily.ANTHROPIC,
                base_url=f"{servers['anthropic']}/v1",
                secret_name="anthropic-api-key",
            ),
            ProviderDefinition(
                id="gemini",
                api_family=ApiFamily.GEMINI,
                base_url=servers["gemini"],
                secret_name="gemini-api-key",
            ),
            ProviderDefinition(
                id="openai",
                api_family=ApiFamily.OPENAI_COMPATIBLE,
                base_url=f"{servers['openai']}/v1",
                secret_name="openai-api-key",
            ),
        ),
    )
    models = ModelService(
        registry, vault, kernel, operations, endpoints, recovery_repository
    )
    return operations, kernel, models, vault


def make_operation(service: OperationService) -> str:
    return service.create(
        "Families lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("example.com",))
    ).id


def complete(
    models: ModelService,
    kernel: KernelService,
    operation_id: str,
    model_id: str,
    prompt: str,
    **kwargs: object,
) -> ChatResponse:
    approval_id = kwargs.pop("approval_id", None)
    if approval_id is None:
        approval = kernel.request_approval(
            operation_id, f"model.call {model_id}", RiskClass.R2
        )
        kernel.approve(approval.id, decided_by="operator")
        approval_id = approval.id
    return models.complete(
        operation_id, model_id, prompt, approval_id=approval_id, **kwargs
    )


def test_anthropic_complete_flow(vault_env: Path, servers: dict[str, str]) -> None:
    service, kernel, models, vault = make_stack(vault_env / "anthropic.db", servers)
    vault.add("anthropic-api-key", ANTHROPIC_KEY)
    operation_id = make_operation(service)
    response = complete(models, kernel, operation_id, _ANTHROPIC_MODEL, "contract prompt")
    assert response.text == "anthropic:contract prompt"
    assert response.usage.prompt_tokens == 8
    assert response.usage.completion_tokens == 4
    events = service.events(operation_id)
    assert ANTHROPIC_KEY not in json.dumps([event.payload for event in events])
    calls = [event for event in events if event.event_type == "model.call"]
    assert calls[0].payload["provider_id"] == "anthropic"


def test_gemini_complete_flow(vault_env: Path, servers: dict[str, str]) -> None:
    service, kernel, models, vault = make_stack(vault_env / "gemini.db", servers)
    vault.add("gemini-api-key", GEMINI_KEY)
    operation_id = make_operation(service)
    response = complete(models, kernel, operation_id, _GEMINI_MODEL, "contract prompt")
    assert response.text == "gemini:contract prompt"
    assert response.usage.prompt_tokens == 6
    events = service.events(operation_id)
    assert GEMINI_KEY not in json.dumps([event.payload for event in events])
    calls = [event for event in events if event.event_type == "model.call"]
    assert calls[0].payload["provider_id"] == "gemini"


def test_contract_same_shape_across_families(
    vault_env: Path, servers: dict[str, str]
) -> None:
    service, kernel, models, vault = make_stack(vault_env / "contract.db", servers)
    vault.add("anthropic-api-key", ANTHROPIC_KEY)
    vault.add("gemini-api-key", GEMINI_KEY)
    vault.add("openai-api-key", OPENAI_KEY)
    operation_id = make_operation(service)

    responses = {}
    for model_id, family in (
        (_OPENAI_MODEL, "openai"),
        (_ANTHROPIC_MODEL, "anthropic"),
        (_GEMINI_MODEL, "gemini"),
    ):
        responses[family] = complete(models, kernel, operation_id, model_id, "contract prompt")

    for family, response in responses.items():
        assert response.text == f"{family}:contract prompt"
        assert response.provider_id == family
        assert response.usage.prompt_tokens > 0
        assert response.usage.completion_tokens > 0
        assert response.latency_ms >= 0
        assert isinstance(response.tool_calls, tuple)


def test_fallback_chain_openai_to_anthropic_on_5xx(
    vault_env: Path, servers: dict[str, str]
) -> None:
    service, kernel, models, vault = make_stack(vault_env / "fallback.db", servers)
    vault.add("openai-api-key", "wrong-key")
    vault.add("anthropic-api-key", ANTHROPIC_KEY)
    operation_id = make_operation(service)

    response = complete(
        models,
        kernel,
        operation_id,
        _OPENAI_MODEL,
        "contract prompt",
        fallbacks=(_ANTHROPIC_MODEL,),
    )
    assert response.text == "anthropic:contract prompt"
    assert response.model_id == _ANTHROPIC_MODEL

    events = service.events(operation_id)
    fallbacks = [event for event in events if event.event_type == "model.fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0].payload == {
        "from_model": _OPENAI_MODEL,
        "to_model": _ANTHROPIC_MODEL,
        "reason": "auth",
    }
    calls = [event for event in events if event.event_type == "model.call"]
    assert calls[0].payload["model_id"] == _ANTHROPIC_MODEL
    assert OPENAI_KEY not in json.dumps([event.payload for event in events])


def test_fallback_chain_three_families(
    vault_env: Path, servers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, models, vault = make_stack(vault_env / "fallback3.db", servers)
    vault.add("openai-api-key", "wrong-key")
    vault.add("anthropic-api-key", ANTHROPIC_KEY)
    vault.add("gemini-api-key", GEMINI_KEY)
    operation_id = make_operation(service)
    _AnthropicHandler.fail_with_500 = True
    try:
        response = complete(
            models,
            kernel,
            operation_id,
            _OPENAI_MODEL,
            "contract prompt",
            fallbacks=(_ANTHROPIC_MODEL, _GEMINI_MODEL),
        )
    finally:
        _AnthropicHandler.fail_with_500 = False
    assert response.text == "gemini:contract prompt"
    events = service.events(operation_id)
    fallbacks = [event for event in events if event.event_type == "model.fallback"]
    assert [event.payload["reason"] for event in fallbacks] == ["auth", "unavailable"]
    assert fallbacks[1].payload["to_model"] == _GEMINI_MODEL


def test_budget_exceeded_blocks_before_call(
    vault_env: Path, servers: dict[str, str]
) -> None:
    service, kernel, models, vault = make_stack(vault_env / "budget.db", servers)
    vault.add("anthropic-api-key", ANTHROPIC_KEY)
    operation_id = make_operation(service)
    models.set_budget(operation_id, ModelBudget(max_calls=1))

    response = complete(models, kernel, operation_id, _ANTHROPIC_MODEL, "first")
    assert response.text == "anthropic:first"
    with pytest.raises(BudgetExceededError, match="calls"):
        complete(models, kernel, operation_id, _ANTHROPIC_MODEL, "second")
    events = service.events(operation_id)
    calls = [event for event in events if event.event_type == "model.call"]
    assert len(calls) == 1

    summary = models.usage(operation_id)
    assert summary.calls == 1
    assert summary.prompt_tokens == 8
    assert summary.by_model[_ANTHROPIC_MODEL]["calls"] == 1


def test_budget_not_set_allows_calls(vault_env: Path, servers: dict[str, str]) -> None:
    service, kernel, models, vault = make_stack(vault_env / "nobudget.db", servers)
    vault.add("gemini-api-key", GEMINI_KEY)
    operation_id = make_operation(service)
    complete(models, kernel, operation_id, _GEMINI_MODEL, "one")
    complete(models, kernel, operation_id, _GEMINI_MODEL, "two")
    summary = models.usage(operation_id)
    assert summary.calls == 2
    assert summary.completion_tokens == 4


def test_token_budget_blocks_second_call(
    vault_env: Path, servers: dict[str, str]
) -> None:
    service, kernel, models, vault = make_stack(vault_env / "tokenbudget.db", servers)
    vault.add("openai-api-key", OPENAI_KEY)
    operation_id = make_operation(service)
    models.set_budget(operation_id, ModelBudget(max_tokens_total=8))
    complete(models, kernel, operation_id, _OPENAI_MODEL, "first")
    with pytest.raises(BudgetExceededError, match="tokens"):
        complete(models, kernel, operation_id, _OPENAI_MODEL, "second")


def test_anthropic_missing_key_falls_back_with_auth_reason(
    vault_env: Path, servers: dict[str, str]
) -> None:
    service, kernel, models, vault = make_stack(vault_env / "nokey.db", servers)
    vault.add("gemini-api-key", GEMINI_KEY)
    operation_id = make_operation(service)
    response = complete(
        models,
        kernel,
        operation_id,
        _ANTHROPIC_MODEL,
        "contract prompt",
        fallbacks=(_GEMINI_MODEL,),
    )
    assert response.text == "gemini:contract prompt"
    events = service.events(operation_id)
    fallbacks = [event for event in events if event.event_type == "model.fallback"]
    assert fallbacks[0].payload["reason"] == "auth"


def test_approval_required_before_any_call(
    vault_env: Path, servers: dict[str, str]
) -> None:
    service, _, models, vault = make_stack(vault_env / "approval.db", servers)
    vault.add("anthropic-api-key", ANTHROPIC_KEY)
    operation_id = make_operation(service)
    with pytest.raises(ApprovalRequiredError):
        models.complete(operation_id, _ANTHROPIC_MODEL, "hi")
    events = service.events(operation_id)
    assert not [event for event in events if event.event_type == "model.call"]
