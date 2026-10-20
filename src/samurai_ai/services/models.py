from __future__ import annotations

import asyncio
from typing import Any

from samurai_ai.agents.ollama import OllamaProvider
from samurai_ai.agents.openai_compat import OpenAICompatProvider
from samurai_ai.agents.provider import ChatRequest, ChatResponse, Message
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.execution import ApprovalRequiredError, ToolStateError
from samurai_ai.domain.kernel import Approval, ApprovalStatus, RiskClass
from samurai_ai.domain.models import (
    ApiFamily,
    ModelDefinition,
    ModelError,
    ModelNotFoundError,
    ProviderDefinition,
    parse_model_id,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.vault import SecretNotFoundError
from samurai_ai.services.kernel import ApprovalNotFoundError, KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.models import ModelEndpointRepository


class ModelService:
    def __init__(
        self,
        registry: ModelRegistry,
        vault: VaultService,
        kernel: KernelService,
        operations: OperationService,
        endpoints: ModelEndpointRepository | None = None,
    ) -> None:
        self.registry = registry
        self.vault = vault
        self.kernel = kernel
        self.operations = operations
        self.endpoints = endpoints

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
        if provider.api_family is ApiFamily.OLLAMA:
            return asyncio.run(OllamaProvider(provider).list_models())
        if provider.api_family is ApiFamily.OPENAI_COMPATIBLE:
            impl = self._build_provider(provider)
            names = asyncio.run(impl.list_models())
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
        raise ModelError(f"discovery is not supported for {provider.api_family.value}")

    def complete(
        self,
        operation_id: str,
        model_id: str,
        prompt: str,
        *,
        max_tokens: int | None = None,
        approval_id: str | None = None,
    ) -> ChatResponse:
        self.operations.get(operation_id)
        model, provider = self._resolve_model(model_id)
        self._resolve_approval(operation_id, model, provider, approval_id)
        request = ChatRequest(
            model_id=model.id,
            messages=(Message(role="user", content=prompt),),
            max_tokens=max_tokens,
        )
        response = asyncio.run(self._complete(provider, request))
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type="model.call",
                payload={
                    "model_id": model.id,
                    "provider_id": provider.id,
                    "local": provider.local,
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "latency_ms": response.latency_ms,
                    "estimated_cost_usd": model.estimate_cost(
                        response.usage.prompt_tokens,
                        response.usage.completion_tokens,
                    ),
                },
            )
        )
        return response

    async def _complete(
        self, provider: ProviderDefinition, request: Any
    ) -> ChatResponse:
        if provider.api_family is ApiFamily.OLLAMA:
            return await OllamaProvider(provider).complete(request)
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

    def _resolve_approval(
        self,
        operation_id: str,
        model: ModelDefinition,
        provider: ProviderDefinition,
        approval_id: str | None,
    ) -> Approval:
        risk = RiskClass.R1 if provider.local else RiskClass.R2
        action = f"model.call {model.id}"
        if approval_id is None:
            approval = self.kernel.request_approval(
                operation_id,
                action,
                risk,
                reason=f"{provider.id} via {provider.base_url}",
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

    def _build_provider(self, provider: ProviderDefinition) -> OpenAICompatProvider:
        api_key: str | None = None
        if provider.secret_name:
            try:
                api_key = self.vault.get(provider.secret_name)
            except SecretNotFoundError as exc:
                raise ModelError(
                    f"API key for provider {provider.id} is not configured; "
                    f"add it with 'samurai secret add {provider.secret_name} --stdin'"
                ) from exc
        return OpenAICompatProvider(provider, api_key)

    def _secret_exists(self, secret_name: str) -> bool:
        try:
            self.vault.get(secret_name)
        except Exception:
            return False
        return True
