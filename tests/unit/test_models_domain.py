import pytest

from samurai_ai.agents.capabilities import require_capabilities, select_model
from samurai_ai.agents.ollama import (
    build_chat_payload,
    infer_capabilities,
    model_definition_for,
    parse_chat_response,
)
from samurai_ai.agents.openai_compat import (
    broker_for_base_url,
    build_completion_payload,
    parse_completion,
)
from samurai_ai.agents.provider import ChatRequest, Message, ToolSpec
from samurai_ai.agents.registry import STATIC_MODELS, STATIC_PROVIDERS, ModelRegistry
from samurai_ai.domain.models import (
    ApiFamily,
    CapabilityError,
    ModelCapability,
    ModelDefinition,
    ModelError,
    ModelNotFoundError,
    ProviderDefinition,
    parse_model_id,
)


def test_parse_model_id() -> None:
    assert parse_model_id("openai/gpt-4o@v1") == ("openai", "gpt-4o")
    assert parse_model_id("ollama/llama3.1:8b@v2") == ("ollama", "llama3.1:8b")
    for bad in ("gpt-4o", "openai/gpt-4o", "opena i/g@v1", "openai/@v1"):
        with pytest.raises(ModelError, match="invalid model id"):
            parse_model_id(bad)


def test_provider_base_url_strips_completion_suffix() -> None:
    provider = ProviderDefinition(
        id="giga",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://api.giga.chat/osav1/chat/completions",
    )
    assert provider.base_url == "https://api.giga.chat/osav1"
    plain = ProviderDefinition(
        id="x2",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="https://api.example.com/v1/",
    )
    assert plain.base_url == "https://api.example.com/v1"


def test_parse_model_id_with_slashed_model_name() -> None:
    assert parse_model_id("giga/giga-osa/deepseek-v4.1-flash-uncensored@v1") == (
        "giga",
        "giga-osa/deepseek-v4.1-flash-uncensored",
    )
    assert parse_model_id("openrouter/anthropic/claude-sonnet-4@v1") == (
        "openrouter",
        "anthropic/claude-sonnet-4",
    )
    model = ModelDefinition(
        id="giga/giga-osa/deepseek-v4.1-flash-uncensored@v1",
        provider_id="giga",
    )
    assert model.model_name == "giga-osa/deepseek-v4.1-flash-uncensored"


def test_static_registry_contents() -> None:
    registry = ModelRegistry()
    providers = {provider.id for provider in STATIC_PROVIDERS}
    assert {
        "openai",
        "deepseek",
        "xai",
        "moonshot",
        "mistral",
        "openrouter",
        "qwen",
        "ollama",
    } <= providers
    ollama = registry.get_provider("ollama")
    assert ollama.local is True
    assert ollama.secret_name is None
    remote = registry.get_provider("openai")
    assert remote.local is False
    assert remote.api_family is ApiFamily.OPENAI_COMPATIBLE

    model, provider = registry.get_model("openai/gpt-4o@v1")
    assert provider.id == "openai"
    assert ModelCapability.TOOLS in model.capabilities
    assert ModelCapability.VISION in model.capabilities
    assert model.model_name == "gpt-4o"


def test_registry_fail_closed_for_unknown() -> None:
    registry = ModelRegistry()
    with pytest.raises(ModelNotFoundError):
        registry.get_provider("no-such-provider")
    with pytest.raises(ModelNotFoundError):
        registry.get_model("openai/gpt-3@v1")
    with pytest.raises(ModelNotFoundError):
        registry.get_model("unknown/model@v1")
    with pytest.raises(ModelError):
        registry.get_model("not-a-model-id")


def test_ollama_models_are_not_static() -> None:
    registry = ModelRegistry()
    with pytest.raises(ModelNotFoundError, match="discovered"):
        registry.get_model("ollama/llama3.1:8b@v1")
    assert registry.list_models("ollama") == []
    assert len(STATIC_MODELS) >= 8


def test_require_capabilities_fail_closed() -> None:
    model = STATIC_MODELS[0]
    require_capabilities(model, (ModelCapability.TOOLS,))
    with pytest.raises(CapabilityError) as caught:
        require_capabilities(_no_tools_model(), (ModelCapability.TOOLS,))
    assert caught.value.missing == ("tools",)


def _no_tools_model() -> ModelDefinition:
    return ModelDefinition(
        id="custom/plain@v1",
        provider_id="custom",
        capabilities=frozenset({ModelCapability.STREAMING}),
    )


def test_select_model_prefers_cheapest() -> None:
    registry = ModelRegistry()
    selected = select_model(registry, (ModelCapability.TOOLS,))
    assert selected.id == "openrouter/auto@v1"  # cost 0/0 wins the cost sort
    priced = [
        model
        for model in registry.list_models()
        if model.cost_input_per_m + model.cost_output_per_m > 0
        and ModelCapability.TOOLS in model.capabilities
    ]
    assert min(priced, key=lambda m: m.cost_input_per_m + m.cost_output_per_m).id == (
        "anthropic/claude-haiku-5-5@v1"
    )
    local = select_model(registry, (), prefer_local=True)
    assert local.local is False  # no static local models; ollama is discovery-only
    vision = select_model(registry, (ModelCapability.VISION,))
    assert vision.id  # any vision-capable candidate is fine; ids move with releases


def test_select_model_fail_closed_when_nothing_matches() -> None:
    class EmptyRegistry:
        def list_models(self, provider_id: str | None = None) -> list[ModelDefinition]:
            return []

    with pytest.raises(ModelNotFoundError):
        select_model(EmptyRegistry(), ())  # type: ignore[arg-type]


def test_build_completion_payload_shape() -> None:
    request = ChatRequest(
        model_id="openai/gpt-4o@v1",
        messages=(Message(role="user", content="hi"),),
        tools=(
            ToolSpec(
                name="search",
                description="Search",
                parameters={"type": "object", "properties": {}},
            ),
        ),
        max_tokens=128,
        temperature=0.2,
    )
    payload = build_completion_payload("gpt-4o", request)
    assert payload["model"] == "gpt-4o"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["max_tokens"] == 128
    assert payload["temperature"] == 0.2
    assert payload["tools"][0]["function"]["name"] == "search"


def test_parse_completion_with_tool_calls() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "search", "arguments": '{"q": "x"}'},
                        },
                        {
                            "id": "call_2",
                            "function": {"name": "bad", "arguments": "not-json"},
                        },
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    response = parse_completion("openai/gpt-4o@v1", "openai", payload, 42)
    assert response.text == ""
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"q": "x"}
    assert response.tool_calls[1].arguments == {}
    assert response.usage.prompt_tokens == 11
    assert response.usage.total_tokens == 18
    assert response.latency_ms == 42


def test_parse_completion_requires_choices() -> None:
    with pytest.raises(ModelError, match="no choices"):
        parse_completion("openai/gpt-4o@v1", "openai", {}, 0)


def test_ollama_capability_heuristics() -> None:
    assert ModelCapability.TOOLS in infer_capabilities("llama3.1:8b")
    assert ModelCapability.TOOLS in infer_capabilities("qwen2.5-coder:7b")
    assert ModelCapability.TOOLS not in infer_capabilities("phi-2:latest")
    model = model_definition_for("llama3.1:8b")
    assert model.local is True
    assert model.api_family is ApiFamily.OLLAMA
    assert model.cost_input_per_m == 0.0


def test_ollama_chat_mapping() -> None:
    request = ChatRequest(
        model_id="ollama/llama3.1:8b@v1",
        messages=(
            Message(role="system", content="sys"),
            Message(role="user", content="hi"),
        ),
        max_tokens=64,
    )
    payload = build_chat_payload("llama3.1:8b", request)
    assert payload["stream"] is False
    assert payload["options"]["num_predict"] == 64
    assert len(payload["messages"]) == 2

    response = parse_chat_response(
        "ollama/llama3.1:8b@v1",
        {
            "message": {"role": "assistant", "content": "hello"},
            "prompt_eval_count": 9,
            "eval_count": 4,
        },
        17,
    )
    assert response.text == "hello"
    assert response.usage.prompt_tokens == 9
    assert response.usage.completion_tokens == 4
    assert response.provider_id == "ollama"


def test_broker_for_base_url_restricts_to_registered_host() -> None:
    broker = broker_for_base_url("https://api.openai.com/v1")
    assert broker.scope.evaluate("https://api.openai.com/v1/models").allowed
    assert not broker.scope.evaluate("https://evil.example.com/").allowed
    with pytest.raises(ModelError):
        broker_for_base_url("not-a-url")


def test_estimate_cost() -> None:
    model = STATIC_MODELS[0]
    cost = model.estimate_cost(1_000_000, 1_000_000)
    assert cost == round(model.cost_input_per_m + model.cost_output_per_m, 6)
