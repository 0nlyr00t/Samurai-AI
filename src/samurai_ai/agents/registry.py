"""Versioned static registry of LLM providers and models.

Static, code-defined providers (OpenAI-compatible family) plus the local
Ollama provider. Custom OpenAI-compatible endpoints (migration v7) and
operator-whitelisted custom model names (migration v15) registered via CLI
are persisted in SQLite and merged here.
Fail-closed: unknown provider or model ids raise ModelNotFoundError.
"""

from __future__ import annotations

from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelDefinition,
    ModelNotFoundError,
    ProviderDefinition,
    parse_model_id,
)
from samurai_ai.storage.models import CustomModelRepository, ModelEndpointRepository

STATIC_PROVIDERS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        id="openai",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://api.openai.com/v1",
        secret_name="openai-api-key",
    ),
    ProviderDefinition(
        id="deepseek",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://api.deepseek.com/v1",
        secret_name="deepseek-api-key",
    ),
    ProviderDefinition(
        id="xai",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://api.x.ai/v1",
        secret_name="xai-api-key",
    ),
    ProviderDefinition(
        id="moonshot",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://api.moonshot.ai/v1",
        secret_name="moonshot-api-key",
    ),
    ProviderDefinition(
        id="mistral",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://api.mistral.ai/v1",
        secret_name="mistral-api-key",
    ),
    ProviderDefinition(
        id="openrouter",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://openrouter.ai/api/v1",
        secret_name="openrouter-api-key",
    ),
    ProviderDefinition(
        id="qwen",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        secret_name="dashscope-api-key",
    ),
    ProviderDefinition(
        id="ollama",
        api_family=ApiFamily.OLLAMA,
        base_url="http://localhost:11434",
        local=True,
    ),
    ProviderDefinition(
        id="anthropic",
        api_family=ApiFamily.ANTHROPIC,
        base_url="https://api.anthropic.com/v1",
        secret_name="anthropic-api-key",
    ),
    ProviderDefinition(
        id="zhipu",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://open.bigmodel.cn/api/paas/v4",
        secret_name="zhipu-api-key",
    ),
    ProviderDefinition(
        id="gemini",
        api_family=ApiFamily.GEMINI,
        base_url="https://generativelanguage.googleapis.com",
        secret_name="gemini-api-key",
    ),
)

_TOOL_CAPS = frozenset(
    {
        ModelCapability.TOOLS,
        ModelCapability.STRUCTURED_OUTPUT,
        ModelCapability.STREAMING,
    }
)

STATIC_MODELS: tuple[ModelDefinition, ...] = (
    ModelDefinition(
        id="openai/gpt-4o@v1",
        provider_id="openai",
        display_name="GPT-4o",
        context_limit=128000,
        max_output_tokens=16384,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=2.5,
        cost_output_per_m=10.0,
    ),
    ModelDefinition(
        id="openai/gpt-4o-mini@v1",
        provider_id="openai",
        display_name="GPT-4o mini",
        context_limit=128000,
        max_output_tokens=16384,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=0.15,
        cost_output_per_m=0.6,
    ),
    ModelDefinition(
        id="deepseek/deepseek-chat@v1",
        provider_id="deepseek",
        display_name="DeepSeek Chat",
        context_limit=64000,
        max_output_tokens=8192,
        capabilities=_TOOL_CAPS,
        cost_input_per_m=0.27,
        cost_output_per_m=1.1,
    ),
    ModelDefinition(
        id="deepseek/deepseek-reasoner@v1",
        provider_id="deepseek",
        display_name="DeepSeek Reasoner",
        context_limit=64000,
        max_output_tokens=8192,
        capabilities=frozenset({ModelCapability.STREAMING}),
        cost_input_per_m=0.55,
        cost_output_per_m=2.19,
    ),
    ModelDefinition(
        id="xai/grok-3@v1",
        provider_id="xai",
        display_name="Grok 3",
        context_limit=131072,
        max_output_tokens=8192,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.STREAMING}),
        cost_input_per_m=3.0,
        cost_output_per_m=15.0,
    ),
    ModelDefinition(
        id="moonshot/kimi-k2@v1",
        provider_id="moonshot",
        display_name="Kimi K2",
        context_limit=131072,
        max_output_tokens=8192,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.STREAMING}),
        cost_input_per_m=0.6,
        cost_output_per_m=2.5,
    ),
    ModelDefinition(
        id="moonshot/kimi-k2-thinking@v1",
        provider_id="moonshot",
        display_name="Kimi K2 Thinking",
        context_limit=262144,
        max_output_tokens=16384,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.STREAMING}),
        cost_input_per_m=0.6,
        cost_output_per_m=2.5,
    ),
    ModelDefinition(
        id="mistral/mistral-large@v1",
        provider_id="mistral",
        display_name="Mistral Large",
        context_limit=131072,
        max_output_tokens=8192,
        capabilities=_TOOL_CAPS,
        cost_input_per_m=2.0,
        cost_output_per_m=6.0,
    ),
    ModelDefinition(
        id="openrouter/auto@v1",
        provider_id="openrouter",
        display_name="OpenRouter Auto",
        context_limit=131072,
        max_output_tokens=4096,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.STREAMING}),
    ),
    ModelDefinition(
        id="qwen/qwen-max@v1",
        provider_id="qwen",
        display_name="Qwen Max",
        context_limit=32768,
        max_output_tokens=8192,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.STREAMING}),
        cost_input_per_m=1.6,
        cost_output_per_m=6.4,
    ),
    ModelDefinition(
        id="anthropic/claude-sonnet-4-20250514@v1",
        provider_id="anthropic",
        display_name="Claude Sonnet 4",
        api_family=ApiFamily.ANTHROPIC,
        context_limit=200000,
        max_output_tokens=8192,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=3.0,
        cost_output_per_m=15.0,
    ),
    ModelDefinition(
        id="anthropic/claude-haiku-4-5-20251001@v1",
        provider_id="anthropic",
        display_name="Claude Haiku 4.5",
        api_family=ApiFamily.ANTHROPIC,
        context_limit=200000,
        max_output_tokens=8192,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=1.0,
        cost_output_per_m=5.0,
    ),
    ModelDefinition(
        id="gemini/gemini-2.5-pro@v1",
        provider_id="gemini",
        display_name="Gemini 2.5 Pro",
        api_family=ApiFamily.GEMINI,
        context_limit=1048576,
        max_output_tokens=8192,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=1.25,
        cost_output_per_m=10.0,
    ),
    ModelDefinition(
        id="gemini/gemini-2.5-flash@v1",
        provider_id="gemini",
        display_name="Gemini 2.5 Flash",
        api_family=ApiFamily.GEMINI,
        context_limit=1048576,
        max_output_tokens=8192,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=0.3,
        cost_output_per_m=2.5,
    ),
    # -- frontier presets, refreshed 2026-11 -------------------------------
    ModelDefinition(
        id="openai/gpt-6-astra@v1",
        provider_id="openai",
        display_name="GPT-6 Astra",
        context_limit=400000,
        max_output_tokens=131072,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=10.0,
        cost_output_per_m=50.0,
    ),
    ModelDefinition(
        id="openai/gpt-6-1-sol@v1",
        provider_id="openai",
        display_name="GPT-6.1 Sol",
        context_limit=400000,
        max_output_tokens=65536,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=4.0,
        cost_output_per_m=20.0,
    ),
    ModelDefinition(
        id="openai/gpt-6-1-terra@v1",
        provider_id="openai",
        display_name="GPT-6.1 Terra",
        context_limit=400000,
        max_output_tokens=32768,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=1.5,
        cost_output_per_m=8.0,
    ),
    ModelDefinition(
        id="anthropic/claude-opus-5-5@v1",
        provider_id="anthropic",
        display_name="Claude Opus 5.5",
        api_family=ApiFamily.ANTHROPIC,
        context_limit=200000,
        max_output_tokens=64000,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=4.0,
        cost_output_per_m=20.0,
    ),
    ModelDefinition(
        id="anthropic/claude-sonnet-5-5@v1",
        provider_id="anthropic",
        display_name="Claude Sonnet 5.5",
        api_family=ApiFamily.ANTHROPIC,
        context_limit=200000,
        max_output_tokens=64000,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=2.0,
        cost_output_per_m=10.0,
    ),
    ModelDefinition(
        id="anthropic/claude-haiku-5-5@v1",
        provider_id="anthropic",
        display_name="Claude Haiku 5.5",
        api_family=ApiFamily.ANTHROPIC,
        context_limit=200000,
        max_output_tokens=32768,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=0.1,
        cost_output_per_m=0.5,
    ),
    ModelDefinition(
        id="anthropic/claude-fable-5-1@v1",
        provider_id="anthropic",
        display_name="Claude Fable 5.1",
        api_family=ApiFamily.ANTHROPIC,
        context_limit=200000,
        max_output_tokens=64000,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=10.0,
        cost_output_per_m=50.0,
    ),
    ModelDefinition(
        id="moonshot/kimi-k3@v1",
        provider_id="moonshot",
        display_name="Kimi K3",
        context_limit=256000,
        max_output_tokens=32768,
        capabilities=_TOOL_CAPS,
        cost_input_per_m=0.6,
        cost_output_per_m=2.5,
    ),
    ModelDefinition(
        id="moonshot/kimi-k2-7-code@v1",
        provider_id="moonshot",
        display_name="Kimi K2.7 Code",
        context_limit=256000,
        max_output_tokens=32768,
        capabilities=_TOOL_CAPS,
        cost_input_per_m=0.4,
        cost_output_per_m=1.8,
    ),
    ModelDefinition(
        id="zhipu/glm-5-1@v1",
        provider_id="zhipu",
        display_name="GLM-5.1",
        context_limit=200000,
        max_output_tokens=98304,
        capabilities=_TOOL_CAPS,
        cost_input_per_m=0.6,
        cost_output_per_m=2.2,
    ),
    ModelDefinition(
        id="zhipu/glm-5-turbo@v1",
        provider_id="zhipu",
        display_name="GLM-5 Turbo",
        context_limit=200000,
        max_output_tokens=32768,
        capabilities=_TOOL_CAPS,
        cost_input_per_m=0.15,
        cost_output_per_m=0.9,
    ),
    ModelDefinition(
        id="deepseek/deepseek-v4-pro@v1",
        provider_id="deepseek",
        display_name="DeepSeek V4 Pro",
        # the V4 family runs a 1M window on current deployments
        context_limit=1000000,
        max_output_tokens=8192,
        capabilities=_TOOL_CAPS,
        cost_input_per_m=0.28,
        cost_output_per_m=0.42,
    ),
    ModelDefinition(
        id="deepseek/deepseek-flash@v1",
        provider_id="deepseek",
        display_name="DeepSeek Flash",
        context_limit=1000000,
        max_output_tokens=8192,
        capabilities=_TOOL_CAPS,
        cost_input_per_m=0.15,
        cost_output_per_m=0.6,
    ),
    ModelDefinition(
        id="xai/grok-4-7@v1",
        provider_id="xai",
        display_name="Grok 4.7",
        context_limit=500000,
        max_output_tokens=32768,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=2.0,
        cost_output_per_m=6.0,
    ),
    ModelDefinition(
        id="gemini/gemini-4-argon@v1",
        provider_id="gemini",
        display_name="Gemini 4 Argon",
        api_family=ApiFamily.GEMINI,
        context_limit=1048576,
        max_output_tokens=65536,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=2.0,
        cost_output_per_m=12.0,
    ),
    ModelDefinition(
        id="gemini/gemini-3-8-flash@v1",
        provider_id="gemini",
        display_name="Gemini 3.8 Flash",
        api_family=ApiFamily.GEMINI,
        context_limit=1048576,
        max_output_tokens=32768,
        capabilities=_TOOL_CAPS | {ModelCapability.VISION},
        cost_input_per_m=0.3,
        cost_output_per_m=2.5,
    ),
)


class ModelRegistry:
    def __init__(
        self,
        endpoints: ModelEndpointRepository | None = None,
        providers: tuple[ProviderDefinition, ...] | None = None,
        models: tuple[ModelDefinition, ...] | None = None,
        custom_models: CustomModelRepository | None = None,
    ) -> None:
        self.endpoints = endpoints
        self.custom_models = custom_models
        self.static_providers = providers if providers is not None else STATIC_PROVIDERS
        self.static_models = models if models is not None else STATIC_MODELS

    def get_provider(self, provider_id: str) -> ProviderDefinition:
        for provider in self.static_providers:
            if provider.id == provider_id:
                return provider
        if self.endpoints is not None:
            endpoint = self.endpoints.get(provider_id)
            if endpoint is not None:
                return endpoint
        raise ModelNotFoundError(f"model provider not found: {provider_id}")

    def get_model(self, model_id: str) -> tuple[ModelDefinition, ProviderDefinition]:
        provider_id, _ = parse_model_id(model_id)
        for model in self.static_models:
            if model.id == model_id:
                return model, self.get_provider(model.provider_id)
        if self.custom_models is not None:
            custom = self.custom_models.get(model_id)
            if custom is not None:
                provider = self.get_provider(custom.provider_id)
                return (
                    custom.model_copy(
                        update={"api_family": provider.api_family, "local": provider.local}
                    ),
                    provider,
                )
        provider = self.get_provider(provider_id)
        if provider.api_family is ApiFamily.OLLAMA:
            raise ModelNotFoundError(
                f"ollama models are discovered at runtime; resolve "
                f"{model_id!r} through the provider discovery"
            )
        if self.endpoints is not None and self.endpoints.get(provider_id) is not None:
            return (
                ModelDefinition(
                    id=model_id,
                    provider_id=provider_id,
                    display_name=model_id,
                    api_family=provider.api_family,
                    context_limit=32768,
                    max_output_tokens=4096,
                    capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT}),
                ),
                provider,
            )
        raise ModelNotFoundError(f"model not found: {model_id}")

    def list_providers(self) -> list[ProviderDefinition]:
        providers = list(self.static_providers)
        if self.endpoints is not None:
            providers.extend(self.endpoints.list_all())
        return providers

    def list_models(self, provider_id: str | None = None) -> list[ModelDefinition]:
        models = [
            model
            for model in self.static_models
            if provider_id is None or model.provider_id == provider_id
        ]
        if self.custom_models is not None:
            for custom in self.custom_models.list_all(provider_id):
                provider = self.get_provider(custom.provider_id)
                models.append(
                    custom.model_copy(
                        update={"api_family": provider.api_family, "local": provider.local}
                    )
                )
        return models
