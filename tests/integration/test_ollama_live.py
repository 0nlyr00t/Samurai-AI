import os
from pathlib import Path

import pytest

from samurai_ai.agents.ollama import OllamaProvider
from samurai_ai.agents.registry import ModelRegistry
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
from samurai_ai.storage.vault import VaultRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("SAMURAI_TEST_OLLAMA") != "1",
    reason="opt-in ollama test (set SAMURAI_TEST_OLLAMA=1)",
)


def make_provider() -> OllamaProvider:
    return OllamaProvider(
        ProviderDefinition(
            id="ollama",
            api_family=ApiFamily.OLLAMA,
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            local=True,
        )
    )


def test_ollama_live_health_and_discovery(tmp_path: Path) -> None:
    import asyncio

    provider = make_provider()
    healthy = asyncio.run(provider.health())
    if not healthy:
        pytest.skip("ollama daemon is not available")
    models = asyncio.run(provider.list_models())
    assert models, "ollama is up but has no models pulled"
    assert all(model.local for model in models)

    chosen = next(
        (entry for entry in models if entry.capabilities), models[0]
    )
    database = Database(tmp_path / "ollama.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    vault = VaultService(
        VaultRepository(database), operations.repository, tmp_path / "vault.key"
    )
    endpoints = ModelEndpointRepository(database)
    service = ModelService(
        ModelRegistry(endpoints, providers=(make_provider().provider,)),
        vault,
        kernel,
        operations,
        endpoints,
    )
    operation = operations.create(
        "Ollama live", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    )
    response = service.complete(
        operation.id, chosen.id, "Reply with the single word: pong", max_tokens=64
    )
    assert response.provider_id == "ollama"
    assert response.text.strip() or response.usage.completion_tokens > 0
    events = operations.events(operation.id)
    calls = [event for event in events if event.event_type == "model.call"]
    assert calls and calls[0].payload["local"] is True
