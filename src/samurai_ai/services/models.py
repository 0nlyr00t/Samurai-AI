from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from typing import Any, Protocol

from samurai_ai.agents.anthropic import AnthropicProvider
from samurai_ai.agents.budgets import (
    BUDGET_KEY_PREFIX,
    ModelBudget,
    UsageSummary,
    check_budget,
    usage_from_events,
)
from samurai_ai.agents.gemini import GeminiProvider
from samurai_ai.agents.ollama import OllamaProvider
from samurai_ai.agents.openai_compat import OpenAICompatProvider
from samurai_ai.agents.provider import ChatRequest, ChatResponse, Message
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.agents.router import ModelRouter
from samurai_ai.domain.execution import ApprovalRequiredError, ToolStateError
from samurai_ai.domain.kernel import Approval, ApprovalStatus, RiskClass
from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelDefinition,
    ModelError,
    ModelNotFoundError,
    ProviderDefinition,
    ProviderUnavailableError,
    parse_model_id,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.vault import SecretNotFoundError
from samurai_ai.services.kernel import ApprovalNotFoundError, KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.models import CustomModelRepository, ModelEndpointRepository
from samurai_ai.storage.recovery import RecoveryRepository


class ProviderImpl(Protocol):
    async def health(self) -> bool: ...

    async def list_models(self) -> Any: ...

    async def complete(self, request: ChatRequest) -> ChatResponse: ...


class ModelService:
    def __init__(
        self,
        registry: ModelRegistry,
        vault: VaultService,
        kernel: KernelService,
        operations: OperationService,
        endpoints: ModelEndpointRepository | None = None,
        settings: RecoveryRepository | None = None,
        provider_factory: Callable[[ProviderDefinition], ProviderImpl] | None = None,
        custom_models: CustomModelRepository | None = None,
    ) -> None:
        self.registry = registry
        self.vault = vault
        self.kernel = kernel
        self.operations = operations
        self.endpoints = endpoints
        self.settings = settings
        self.provider_factory = provider_factory
        self.custom_models = custom_models
        self._budget_locks: dict[str, threading.Lock] = {}
        self._budget_locks_guard = threading.Lock()

    def list_providers(self) -> list[dict[str, Any]]:
        rows = []
        for provider in self.registry.list_providers():
            auth = "local" if provider.local else (
                "configured"
                if provider.secret_name and self._secret_exists(provider.secret_name)
                else ("missing" if provider.secret_name else "none")
            )
            rows.append(
                {
                    "id": provider.id,
                    "api_family": provider.api_family.value,
                    "base_url": provider.base_url,
                    "local": provider.local,
                    "enabled": provider.enabled,
                    "auth": auth,
                    "secret_name": provider.secret_name,
                }
            )
        return rows

    def list_models(self, provider_id: str | None = None) -> list[ModelDefinition]:
        return self.registry.list_models(provider_id)

    def default_model(self) -> str | None:
        if self.settings is None:
            return None
        return self.settings.get_setting("model.default")

    def default_reasoning_effort(self) -> str | None:
        if self.settings is None:
            return None
        value = self.settings.get_setting("model.reasoning_effort")
        return value if value in ("off", "low", "medium", "high") else None

    def register_endpoint(
        self,
        endpoint_id: str,
        base_url: str,
        secret_name: str | None,
        display_name: str = "",
        created_by: str = "operator",
    ) -> ProviderDefinition:
        if self.endpoints is None:
            raise ModelError("model endpoint persistence is not configured")
        endpoint = ProviderDefinition(
            id=endpoint_id,
            api_family=ApiFamily.OPENAI_COMPATIBLE,
            base_url=base_url,
            display_name=display_name,
            secret_name=secret_name,
        )
        self.endpoints.add(endpoint, created_by)
        return endpoint

    def remove_endpoint(self, endpoint_id: str) -> None:
        if self.endpoints is None or not self.endpoints.delete(endpoint_id):
            raise ModelNotFoundError(f"model endpoint not found: {endpoint_id}")

    def add_model(
        self,
        model_id: str,
        display_name: str = "",
        context_limit: int = 32768,
        max_output_tokens: int = 4096,
        created_by: str = "operator",
    ) -> ModelDefinition:
        """Whitelist a provider-side model name not present in the static registry.

        Accepts ids with or without the ``@v<N>`` suffix (defaults to ``@v1``)
        and model names containing slashes (for example
        ``giga/giga-osa/deepseek-v4.1-flash-uncensored``).
        """
        if self.custom_models is None:
            raise ModelError("custom model persistence is not configured")
        if "@" not in model_id:
            model_id = f"{model_id}@v1"
        provider_id, _ = parse_model_id(model_id)
        provider = self.registry.get_provider(provider_id)
        if provider.api_family is ApiFamily.OLLAMA:
            raise ModelError(
                "ollama models are discovered at runtime; no registration needed"
            )
        if any(model.id == model_id for model in self.registry.static_models):
            raise ModelError(f"model already registered: {model_id}")
        model = ModelDefinition(
            id=model_id,
            provider_id=provider_id,
            display_name=display_name or model_id,
            api_family=provider.api_family,
            context_limit=context_limit,
            max_output_tokens=max_output_tokens,
            capabilities=frozenset(
                {
                    ModelCapability.TOOLS,
                    ModelCapability.STRUCTURED_OUTPUT,
                    ModelCapability.STREAMING,
                }
            ),
            local=provider.local,
        )
        self.custom_models.add(model, created_by)
        return model

    def remove_model(self, model_id: str) -> None:
        if "@" not in model_id:
            model_id = f"{model_id}@v1"
        if self.custom_models is None or not self.custom_models.delete(model_id):
            raise ModelNotFoundError(f"custom model not found: {model_id}")

    def health(self, target: str) -> dict[str, Any]:
        try:
            model, provider = self.registry.get_model(target)
        except (ModelNotFoundError, ModelError):
            provider = self.registry.get_provider(target)
            model = None
        impl = self._build_provider(provider)
        healthy = asyncio.run(impl.health())
        return {
            "provider": provider.id,
            "model": model.id if model else None,
            "base_url": provider.base_url,
            "local": provider.local,
            "healthy": healthy,
        }

    def discover_models(self, provider_id: str) -> list[ModelDefinition]:
        provider = self.registry.get_provider(provider_id)
        impl = self._build_provider(provider)
        if provider.api_family is ApiFamily.OLLAMA:
            discovered: list[ModelDefinition] = asyncio.run(impl.list_models())
            return discovered
        names: list[str] = asyncio.run(impl.list_models())
        return [
            ModelDefinition(
                id=f"{provider.id}/{name}@v1",
                provider_id=provider.id,
                display_name=name,
                api_family=provider.api_family,
                context_limit=32768,
                max_output_tokens=4096,
                local=provider.local,
            )
            for name in names
        ]

    def get_budget(self, operation_id: str) -> ModelBudget:
        if self.settings is None:
            return ModelBudget()
        raw = self.settings.get_setting(f"{BUDGET_KEY_PREFIX}{operation_id}")
        if raw is None:
            return ModelBudget()
        return ModelBudget.model_validate(json.loads(raw))

    def set_budget(self, operation_id: str, budget: ModelBudget) -> ModelBudget:
        self.operations.get(operation_id)
        if budget.is_unset():
            raise ModelError("at least one budget limit must be set")
        if self.settings is None:
            raise ModelError("budget persistence is not configured")
        self.settings.set_setting(
            f"{BUDGET_KEY_PREFIX}{operation_id}",
            budget.model_dump_json(),
        )
        return budget

    def usage(self, operation_id: str) -> UsageSummary:
        self.operations.get(operation_id)
        return usage_from_events(self.operations.events(operation_id))

    def complete(
        self,
        operation_id: str,
        model_id: str,
        prompt: str,
        *,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        approval_id: str | None = None,
        fallbacks: tuple[str, ...] = (),
        router: ModelRouter | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> ChatResponse:
        self.operations.get(operation_id)
        model, provider = self._resolve_model(model_id)
        candidates = [model.id, *fallbacks]
        risk = RiskClass.R2 if self._any_remote(candidates) else RiskClass.R1
        self._check_budget(operation_id)
        self._resolve_approval(operation_id, model, risk, approval_id)
        request = ChatRequest(
            model_id=model.id,
            messages=(Message(role="user", content=prompt),),
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort or self.default_reasoning_effort(),
        )

        async def invoke(candidate_id: str) -> ChatResponse:
            candidate_model, candidate_provider = await self._resolve_model_async(
                candidate_id
            )
            self._check_budget(operation_id)
            return await self._complete(
                candidate_provider,
                ChatRequest(
                    model_id=candidate_model.id,
                    messages=request.messages,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    reasoning_effort=request.reasoning_effort,
                ),
            )

        def on_fallback(from_model: str, to_model: str, reason: str) -> None:
            self.operations.repository.append_event(
                OperationEvent(
                    operation_id=operation_id,
                    event_type="model.fallback",
                    payload={
                        "from_model": from_model,
                        "to_model": to_model,
                        "reason": reason,
                    },
                )
            )

        active_router = router or ModelRouter()
        response = asyncio.run(active_router.complete(candidates, invoke, on_fallback))
        used_model, used_provider = self._resolve_model(response.model_id)
        payload: dict[str, Any] = {
            "model_id": response.model_id,
            "provider_id": used_provider.id,
            "local": used_provider.local,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "latency_ms": response.latency_ms,
            "estimated_cost_usd": used_model.estimate_cost(
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
            ),
        }
        if extra_payload:
            payload.update(extra_payload)
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type="model.call",
                payload=payload,
            )
        )
        return response

    def _check_budget(self, operation_id: str) -> None:
        # Serialized per operation so concurrent swarm members cannot interleave
        # usage reads. The check is still not atomic with the subsequent model
        # call: under parallelism the budget may be exceeded by at most the
        # number of in-flight calls, and every call is recorded in usage.
        with self._budget_lock(operation_id):
            budget = self.get_budget(operation_id)
            if budget.is_unset():
                return
            check_budget(budget, self.usage(operation_id))

    def _budget_lock(self, operation_id: str) -> threading.Lock:
        with self._budget_locks_guard:
            lock = self._budget_locks.get(operation_id)
            if lock is None:
                lock = threading.Lock()
                self._budget_locks[operation_id] = lock
            return lock

    def _any_remote(self, candidates: list[str]) -> bool:
        for candidate in candidates:
            provider_id, _ = parse_model_id(candidate)
            provider = self.registry.get_provider(provider_id)
            if not provider.local:
                return True
        return False

    async def _complete(
        self, provider: ProviderDefinition, request: ChatRequest
    ) -> ChatResponse:
        impl = self._build_provider(provider)
        return await impl.complete(request)

    def _resolve_model(
        self, model_id: str
    ) -> tuple[ModelDefinition, ProviderDefinition]:
        provider_id, _ = parse_model_id(model_id)
        provider = self.registry.get_provider(provider_id)
        if provider.api_family is ApiFamily.OLLAMA:
            model = asyncio.run(OllamaProvider(provider).resolve_model(model_id))
            return model, provider
        return self.registry.get_model(model_id)

    def resolve_model(self, model_id: str) -> tuple[ModelDefinition, ProviderDefinition]:
        return self._resolve_model(model_id)

    async def _resolve_model_async(
        self, model_id: str
    ) -> tuple[ModelDefinition, ProviderDefinition]:
        provider_id, _ = parse_model_id(model_id)
        provider = self.registry.get_provider(provider_id)
        if provider.api_family is ApiFamily.OLLAMA:
            model = await OllamaProvider(provider).resolve_model(model_id)
            return model, provider
        return self.registry.get_model(model_id)

    def _resolve_approval(
        self,
        operation_id: str,
        model: ModelDefinition,
        risk: RiskClass,
        approval_id: str | None,
    ) -> Approval:
        action = f"model.call {model.id}"
        if approval_id is None:
            approval = self.kernel.request_approval(
                operation_id,
                action,
                risk,
                reason=f"{model.id} via {model.provider_id}",
                requested_by="operator",
            )
            if approval.status != ApprovalStatus.APPROVED:
                raise ApprovalRequiredError(approval.id)
            return approval
        stored = self.kernel.kernel.get_approval(approval_id)
        if stored is None:
            raise ApprovalNotFoundError(f"approval not found: {approval_id}")
        if stored.operation_id != operation_id:
            raise ToolStateError(
                f"approval {approval_id} belongs to operation {stored.operation_id}"
            )
        if stored.action != action:
            raise ToolStateError(
                f"approval {approval_id} action {stored.action!r} does not match {action!r}"
            )
        if stored.status is ApprovalStatus.PENDING:
            raise ApprovalRequiredError(approval_id)
        if stored.status is ApprovalStatus.REJECTED:
            raise ToolStateError(f"approval {approval_id} was rejected")
        return stored

    def _build_provider(self, provider: ProviderDefinition) -> ProviderImpl:
        if self.provider_factory is not None:
            return self.provider_factory(provider)
        if provider.api_family is ApiFamily.OLLAMA:
            return OllamaProvider(provider)
        api_key: str | None = None
        if provider.secret_name:
            try:
                api_key = self.vault.get(provider.secret_name)
            except SecretNotFoundError as exc:
                raise ProviderUnavailableError(
                    f"API key for provider {provider.id} is not configured; "
                    f"add it with 'samurai secret add {provider.secret_name} --stdin'",
                    auth=True,
                ) from exc
        if provider.api_family is ApiFamily.ANTHROPIC:
            return AnthropicProvider(provider, api_key)
        if provider.api_family is ApiFamily.GEMINI:
            return GeminiProvider(provider, api_key)
        return OpenAICompatProvider(provider, api_key)

    def _secret_exists(self, secret_name: str) -> bool:
        try:
            self.vault.get(secret_name)
        except Exception:
            return False
        return True
