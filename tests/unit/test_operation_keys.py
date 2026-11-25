"""Each operation can bill its own API key; the "own" policy forbids the shared one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from samurai_ai.agents.budgets import usage_from_events
from samurai_ai.agents.provider import ChatRequest, ChatResponse, Message, Usage
from samurai_ai.cli import _make_services
from samurai_ai.domain.execution import ApprovalRequiredError
from samurai_ai.domain.models import ProviderUnavailableError
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract


class _Fake:
    def __init__(self, key: str | None, seen: list[str | None]) -> None:
        self.key = key
        self.seen = seen

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.seen.append(self.key)
        return ChatResponse(
            text="ok",
            usage=Usage(prompt_tokens=10, completion_tokens=2),
            model_id=request.model_id,
            provider_id="gpt",
        )


def _stack(tmp_path: Path) -> tuple[Any, Any, Any, list[str | None]]:
    services = _make_services(tmp_path / "t.db")
    models = services[7]
    operations = models.operations
    vault = models.vault
    vault.add("gpt-key", "shared-key")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/m")
    seen: list[str | None] = []
    models._make_provider = lambda _provider, key: _Fake(key, seen)  # type: ignore[method-assign,assignment,return-value]
    scope = ScopeContract(includes=("*.example.com",))
    a = operations.create("Client A", OperationMode.BUG_BOUNTY, scope)
    b = operations.create("Client B", OperationMode.BUG_BOUNTY, scope)
    return models, a, b, seen


def _call(models: Any, operation_id: str) -> ChatResponse:
    """A remote call is R2: approve it the way the operator would."""
    approval_id: str | None = None
    while True:
        try:
            return models.chat(  # type: ignore[no-any-return]
                operation_id,
                "gpt/m@v1",
                (Message(role="user", content="hi"),),
                approval_id=approval_id,
            )
        except ApprovalRequiredError as exc:
            models.kernel.approve(exc.approval_id, decided_by="operator")
            approval_id = exc.approval_id


def _scopes(models: Any, operation_id: str) -> list[str]:
    return [
        e.payload["key_scope"]
        for e in models.operations.events(operation_id)
        if e.event_type == "model.call"
    ]


def test_an_operation_key_outranks_the_shared_one(tmp_path: Path) -> None:
    models, a, b, seen = _stack(tmp_path)
    models.vault.add("gpt-key", "client-a-key", a.id)

    _call(models, a.id)
    _call(models, b.id)

    assert seen == ["client-a-key", "shared-key"]
    assert _scopes(models, a.id) == ["operation"]
    assert _scopes(models, b.id) == ["global"]
    # the key value never lands in the journal
    assert all("client-a-key" not in str(e.payload) for e in models.operations.events(a.id))


def test_own_key_policy_refuses_the_shared_key(tmp_path: Path) -> None:
    models, a, _b, seen = _stack(tmp_path)
    models.set_key_policy(a.id, require_own=True)
    assert models.requires_own_key(a.id)

    with pytest.raises(ProviderUnavailableError) as caught:
        _call(models, a.id)
    assert caught.value.auth
    assert f"--operation {a.id}" in str(caught.value)
    assert seen == []

    models.vault.add("gpt-key", "client-a-key", a.id)
    _call(models, a.id)
    assert seen == ["client-a-key"]

    models.set_key_policy(a.id, require_own=False)
    assert not models.requires_own_key(a.id)


def test_usage_splits_spend_by_key_scope(tmp_path: Path) -> None:
    models, a, _b, _seen = _stack(tmp_path)
    _call(models, a.id)
    models.vault.add("gpt-key", "client-a-key", a.id)
    _call(models, a.id)
    _call(models, a.id)

    summary = usage_from_events(models.operations.events(a.id))
    assert summary.by_key_scope["global"]["calls"] == 1
    assert summary.by_key_scope["operation"]["calls"] == 2
    assert summary.by_key_scope["operation"]["prompt_tokens"] == 20
