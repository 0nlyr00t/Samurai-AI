"""Ollama gets an explicit window, and Samurai budgets against that same window."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from samurai_ai.agents.ollama import (
    DEFAULT_NUM_CTX,
    OllamaProvider,
    build_chat_payload,
    trained_context_length,
)
from samurai_ai.agents.provider import ChatRequest, Message
from samurai_ai.cli import _make_services
from samurai_ai.domain.models import ApiFamily, ProviderDefinition
from samurai_ai.execution.http import HttpResponse

REQUEST = ChatRequest(model_id="ollama/qwen3:8b@v1", messages=(Message(role="user", content="hi"),))


def test_payload_asks_for_the_window_explicitly() -> None:
    assert build_chat_payload("qwen3:8b", REQUEST, 16384)["options"]["num_ctx"] == 16384
    assert "options" not in build_chat_payload("qwen3:8b", REQUEST)


def test_trained_window_is_read_from_model_info() -> None:
    show = {"model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960}}
    assert trained_context_length(show) == 40960
    assert trained_context_length({"model_info": {}}) is None
    assert trained_context_length({}) is None


class _Broker:
    """Answers /api/tags and /api/show like a local Ollama would."""

    def __init__(self, trained: int) -> None:
        self.trained = trained
        self.paths: list[str] = []

    def fetch(self, url: str, **kwargs: Any) -> HttpResponse:
        import json

        self.paths.append(url.rsplit("/api", 1)[1])
        if url.endswith("/api/tags"):
            payload: dict[str, Any] = {"models": [{"name": "qwen3:8b"}]}
        else:
            payload = {"model_info": {"qwen3.context_length": self.trained}}
        return HttpResponse(
            url=url,
            status=200,
            reason="OK",
            body=json.dumps(payload).encode(),
            truncated=False,
            redirects=(),
        )


def _models(tmp_path: Path, trained: int, monkeypatch: Any) -> tuple[Any, _Broker]:
    services = _make_services(tmp_path / "t.db")
    models = services[7]
    broker = _Broker(trained)
    original = OllamaProvider.__init__

    def init(self: OllamaProvider, provider: ProviderDefinition, *args: Any, **kwargs: Any) -> None:
        kwargs["broker"] = broker
        original(self, provider, *args, **kwargs)

    monkeypatch.setattr(OllamaProvider, "__init__", init)
    return models, broker


def test_the_window_is_the_trained_one_capped(tmp_path: Path, monkeypatch: Any) -> None:
    models, _ = _models(tmp_path, 131072, monkeypatch)
    model, provider = asyncio.run(models._resolve_model_async("ollama/qwen3:8b@v1"))
    assert provider.api_family is ApiFamily.OLLAMA
    assert model.context_limit == DEFAULT_NUM_CTX
    # the UI thread's sync lookup now answers from the cache, without network
    assert models.resolve_model("ollama/qwen3:8b@v1")[0].context_limit == DEFAULT_NUM_CTX


def test_a_small_model_keeps_its_own_window(tmp_path: Path, monkeypatch: Any) -> None:
    models, _ = _models(tmp_path, 8192, monkeypatch)
    model, _ = asyncio.run(models._resolve_model_async("ollama/qwen3:8b@v1"))
    assert model.context_limit == 8192


def test_the_operator_override_wins(tmp_path: Path, monkeypatch: Any) -> None:
    models, _ = _models(tmp_path, 131072, monkeypatch)
    models.settings.set_setting("model.context_limit.ollama/qwen3:8b@v1", "65536")
    model, _ = asyncio.run(models._resolve_model_async("ollama/qwen3:8b@v1"))
    assert model.context_limit == 65536


def test_sync_resolution_inside_the_ui_loop_stays_offline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    models, broker = _models(tmp_path, 131072, monkeypatch)

    async def on_the_ui_thread() -> Any:
        return models.resolve_model("ollama/qwen3:8b@v1")

    model, _ = asyncio.run(on_the_ui_thread())
    assert model.context_limit == DEFAULT_NUM_CTX
    assert broker.paths == []


def test_sync_resolution_outside_a_loop_resolves_live(tmp_path: Path, monkeypatch: Any) -> None:
    models, broker = _models(tmp_path, 8192, monkeypatch)
    model, _ = models.resolve_model("ollama/qwen3:8b@v1")
    assert model.context_limit == 8192
    assert broker.paths == ["/tags", "/show"]
