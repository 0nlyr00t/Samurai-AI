from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

MessageRole = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


#: sent through the token callback when the partial answer is abandoned and the
#: provider is about to resend it from the start
STREAM_RESET = "\x00reset\x00"


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: prompt tokens the provider served from its own prefix cache; gateways that
    #: do not cache leave it at 0 and the gauge simply shows nothing
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit(self) -> float:
        """Share of the prompt that came from cache; 0 when nothing was cached."""
        if not self.prompt_tokens or not self.cached_tokens:
            return 0.0
        return min(1.0, self.cached_tokens / self.prompt_tokens)


class ImagePart(BaseModel):
    """One image riding along with a request; the history stays textual."""

    model_config = ConfigDict(frozen=True)

    mime: str
    data: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    messages: tuple[Message, ...]
    images: tuple[ImagePart, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    response_format: dict[str, Any] | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Field(default_factory=Usage)
    model_id: str
    provider_id: str
    latency_ms: int = 0
    #: reasoning the provider streamed separately from the answer, when it does
    reasoning: str = ""


class ModelProvider(Protocol):
    async def health(self) -> bool: ...

    async def list_models(self) -> list[Any]: ...

    async def complete(self, request: ChatRequest) -> ChatResponse: ...

    async def complete_stream(
        self,
        request: ChatRequest,
        on_token: Callable[[str], None],
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatResponse: ...
