from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

MessageRole = Literal["system", "user", "assistant", "tool"]


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
    #: opaque provider token that must ride back with the call when it is
    #: replayed (Gemini's thoughtSignature); empty for everyone else
    signature: str = ""


class Message(BaseModel):
    """One turn of the conversation.

    Tool traffic is native: an assistant turn carries the calls it made and
    each result is a ``tool`` turn naming the call it answers, so providers
    replay real tool_use/tool_result pairs instead of prose about them.
    """

    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str
    #: assistant turns only: the calls the model made in this turn
    tool_calls: tuple[ToolCall, ...] = ()
    #: tool turns only: the call this result answers and its tool name
    tool_call_id: str = ""
    name: str = ""


class ToolCallDelta(BaseModel):
    """Cumulative raw arguments for one streamed tool call."""

    model_config = ConfigDict(frozen=True)

    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


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
    #: provider stop reason; `length` blocks execution of possibly truncated calls
    stop_reason: str = ""


class ModelProvider(Protocol):
    async def health(self) -> bool: ...

    async def list_models(self) -> list[Any]: ...

    async def complete(self, request: ChatRequest) -> ChatResponse: ...

    async def complete_stream(
        self,
        request: ChatRequest,
        on_token: Callable[[str], None],
        should_stop: Callable[[], bool] | None = None,
        on_tool_delta: Callable[[ToolCallDelta], None] | None = None,
    ) -> ChatResponse: ...


#: the text of a user turn that exists only to carry images past a tool result
IMAGE_CARRIER = "[images attached to the previous tool result]"


def new_call_id(prefix: str = "call_") -> str:
    """A conversation-unique id for a call the provider left unnamed."""
    return f"{prefix}{uuid4().hex[:16]}"


def tool_result(call: ToolCall, content: str) -> Message:
    """The native result turn answering ``call``."""
    return Message(role="tool", content=content, tool_call_id=call.id, name=call.name)


def _call_line(call: ToolCall) -> str:
    arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
    return f"[call {call.name}] {arguments}"


def as_text(message: Message) -> Message:
    """The legacy textual form of a turn: calls become lines, results user notes."""
    if message.role == "assistant" and message.tool_calls:
        lines = [message.content] if message.content.strip() else []
        lines += [_call_line(call) for call in message.tool_calls]
        return Message(role="assistant", content="\n".join(lines))
    if message.role == "tool":
        return Message(role="user", content=message.content)
    return message


def flatten_tool_turns(messages: Sequence[Message]) -> tuple[Message, ...]:
    """History for a request that carries no tool specs.

    A provider may refuse tool_use/tool_result blocks when the request defines
    no tools (Anthropic does), and a one-shot summary or judge call has no use
    for the pairing anyway.
    """
    return tuple(as_text(message) for message in messages)


def repair_tool_pairs(messages: Sequence[Message]) -> tuple[Message, ...]:
    """Every call answered right after its turn, every result tied to a call.

    Trimming, `/undo`, a stop or a reviewer note landing mid-round can leave a
    call without its result, a result without its call, or a note between the
    two. Providers reject all three, so the history is normalised here: results
    move directly behind the turn that made the calls (in call order), a call
    left unanswered gets an explicit placeholder, and a stray result degrades
    to a user note.
    """
    out: list[Message] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        index += 1
        if message.role == "tool":
            out.append(as_text(message))
            continue
        if message.role != "assistant" or not message.tool_calls:
            out.append(message)
            continue
        wanted = {call.id for call in message.tool_calls}
        answers: dict[str, Message] = {}
        others: list[Message] = []
        while index < total and messages[index].role != "assistant":
            follower = messages[index]
            index += 1
            if (
                follower.role == "tool"
                and follower.tool_call_id in wanted
                and follower.tool_call_id not in answers
            ):
                answers[follower.tool_call_id] = follower
            elif follower.role == "tool":
                others.append(as_text(follower))
            else:
                others.append(follower)
        out.append(message)
        for call in message.tool_calls:
            out.append(
                answers.get(call.id)
                or tool_result(call, f"[tool {call.name}] no result: the call was not run")
            )
        out.extend(others)
    return tuple(out)


def wire_messages(request: ChatRequest) -> tuple[Message, ...]:
    """The history a provider should serialise for this request."""
    if not request.tools:
        return flatten_tool_turns(request.messages)
    return repair_tool_pairs(request.messages)


def pair_safe_start(messages: Sequence[Message], start: int) -> int:
    """Move a cut point forward so the kept slice never opens on a lone result."""
    while start < len(messages) and messages[start].role == "tool":
        start += 1
    return start


def split_system(messages: Sequence[Message]) -> tuple[list[str], list[Message]]:
    """The leading system prompt apart from the conversation that follows it.

    APIs with a single top-level ``system`` field (Anthropic, Gemini) used to
    collect *every* system message there, so a memory note added on turn five
    jumped to the top of the prompt — out of place, and a new system prefix
    (a full prompt-cache miss) on every turn that added one. Only the leading
    run is the system prompt; a later one stays where it was, as a reminder.
    """
    system: list[str] = []
    index = 0
    while index < len(messages) and messages[index].role == "system":
        system.append(messages[index].content)
        index += 1
    rest = [
        Message(role="user", content=f"<system-reminder>\n{m.content}\n</system-reminder>")
        if m.role == "system"
        else m
        for m in messages[index:]
    ]
    return system, rest
