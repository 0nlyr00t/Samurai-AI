"""Keeping a long conversation inside the model's window without losing it.

Two moves, cheapest first:

1. **Elision** — old tool results are the bulk of a long session and the least
   useful part of it: the operator and the run record keep the full output, the
   model needs only that the call happened and how it ended. Results older than
   a protected tail are cut to their header line in place, so every call keeps
   its answer and the pairing stays intact.
2. **Compaction** — when elision is not enough, the oldest part of the history
   is folded into one brief. The cut never lands between a call and its result,
   and the brief leads with the operation's proven records (facts, findings)
   so what the model "remembers" stays checkable against the store.

Everything here is pure: callers supply the numbers and the brief's parts.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from samurai_ai.agents.provider import Message, pair_safe_start

#: rough characters per token for mixed English/Russian/code; padded below
CHARS_PER_TOKEN = 4
#: role markers and separators every chat template wraps around a message
MESSAGE_FRAMING_TOKENS = 8
#: window assumed when the model's limit is unknown
FALLBACK_CONTEXT_TOKENS = 64_000
#: start eliding old tool output above this share of the window
ELIDE_AT = 0.5
#: fold old history into a brief above this share of the window
COMPACT_AT = 0.7
#: newest share of the window whose tool output is never elided
PROTECT_SHARE = 0.25
#: newest share of the window a compaction keeps verbatim
KEEP_SHARE = 0.25
#: elide only when it frees at least this share of the window: each pass
#: rewrites history mid-prefix (a prompt-cache miss from that point), so a
#: pass that frees a few hundred tokens every round costs more than it saves
MIN_ELIDE_SHARE = 0.1
#: a result shorter than this is not worth the cache churn of eliding it
MIN_ELIDE_TOKENS = 64
#: the brief may not outgrow this many characters
BRIEF_LIMIT = 6_000

ELIDED_MARK = "[output elided"
COMPACTED_MARK = "[compacted]"


def message_tokens(message: Message) -> int:
    size = len(message.content)
    for call in message.tool_calls:
        size += len(call.name) + len(json.dumps(call.arguments, ensure_ascii=False))
    return size // CHARS_PER_TOKEN + MESSAGE_FRAMING_TOKENS


def estimate_tokens(messages: Sequence[Message]) -> int:
    """A deliberately generous count: overestimating only compacts a bit early."""
    total = sum(message_tokens(message) for message in messages)
    return total + total // 10


@dataclass(frozen=True)
class ContextBudget:
    """Thresholds for one model window, in tokens."""

    limit: int

    @classmethod
    def for_limit(cls, limit: int) -> ContextBudget:
        return cls(limit=limit if limit > 0 else FALLBACK_CONTEXT_TOKENS)

    @property
    def elide_at(self) -> int:
        return int(self.limit * ELIDE_AT)

    @property
    def compact_at(self) -> int:
        return int(self.limit * COMPACT_AT)

    @property
    def protect(self) -> int:
        return int(self.limit * PROTECT_SHARE)

    @property
    def min_elide(self) -> int:
        return int(self.limit * MIN_ELIDE_SHARE)

    @property
    def keep(self) -> int:
        return int(self.limit * KEEP_SHARE)


def elide_tool_outputs(
    messages: Sequence[Message], protect_tokens: int
) -> tuple[list[Message], int]:
    """Old tool results cut to their header line; returns the history and tokens saved.

    The newest ``protect_tokens`` of the history are left alone, as is any
    result already elided or too short to matter.
    """
    out = list(messages)
    seen = 0
    saved = 0
    for index in range(len(out) - 1, -1, -1):
        message = out[index]
        cost = message_tokens(message)
        seen += cost
        if seen <= protect_tokens or message.role != "tool":
            continue
        if ELIDED_MARK in message.content or cost < MIN_ELIDE_TOKENS:
            continue
        header = message.content.split("\n", 1)[0]
        body_tokens = max(0, cost - MESSAGE_FRAMING_TOKENS)
        note = f"{ELIDED_MARK}: ~{body_tokens} tokens; the run record keeps the full output]"
        replacement = message.model_copy(update={"content": f"{header}\n{note}"})
        saved += cost - message_tokens(replacement)
        out[index] = replacement
    return out, saved


def compaction_cut(messages: Sequence[Message], keep_tokens: int) -> int:
    """Index of the first message a compaction keeps verbatim.

    Index 0 is the system prompt and never folds. The kept tail holds at least
    ``keep_tokens`` where the history allows, and it never opens on a result
    whose call would be folded away. A cut that would fold nothing returns 1.
    """
    spent = 0
    cut = len(messages)
    while cut > 1 and spent < keep_tokens:
        cut -= 1
        spent += message_tokens(messages[cut])
    # a long autonomous turn has one operator message at its start, so the cut
    # may land mid-turn; that is fine as long as no result loses its call
    return max(1, pair_safe_start(messages, cut))


def attempted_calls(messages: Sequence[Message], limit: int = 24) -> list[str]:
    """One line per distinct call in the folded region, oldest first."""
    lines: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for call in message.tool_calls:
            arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
            line = f"- {call.name} {arguments[:160]}"
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
    if len(lines) > limit:
        dropped = len(lines) - limit
        lines = [*lines[: limit // 2], f"- … {dropped} more", *lines[-(limit // 2) :]]
    return lines


def build_brief(
    folded: Sequence[Message], evidence: str, narrative: str | None
) -> Message:
    """The one message that stands in for the folded history.

    Records come first and are labelled as such; the narrative, when a model
    wrote one, is labelled as recollection so it never outranks the store.
    """
    parts = [
        f"{COMPACTED_MARK} {len(folded)} earlier messages were folded to save context.",
        "Proven records (from the operation store, authoritative):",
        evidence.strip() or "(none yet)",
    ]
    calls = attempted_calls(folded)
    if calls:
        parts += ["Calls already made (do not repeat them without a reason):", *calls]
    if narrative and narrative.strip():
        parts += ["Recollection of the folded turns (unverified):", narrative.strip()]
    text = "\n".join(parts)
    if len(text) > BRIEF_LIMIT:
        text = text[: BRIEF_LIMIT - 1] + "…"
    return Message(role="user", content=text)


#: smallest output cap worth asking for; below it a tool call cannot fit
MIN_OUTPUT_TOKENS = 1024
#: framing no message count sees (generation prompt, reasoning wrappers)
OUTPUT_HEADWAY = 256


def fit_output_tokens(
    *, limit: int, prompt_tokens: int, model_max: int, required: bool
) -> int | None:
    """The output cap for one request, or None to leave the provider default.

    ``required`` is for APIs that demand a cap (Anthropic): they get the
    model's own maximum, trimmed to the room left in the window. Others keep
    their default unless the window is too full for the model's maximum, when
    an explicit cap turns a hard overflow refusal into a shorter answer.
    """
    if limit <= 0:
        return model_max if required else None
    room = limit - prompt_tokens - OUTPUT_HEADWAY
    if required:
        return max(MIN_OUTPUT_TOKENS, min(model_max, room))
    if room < model_max:
        return max(MIN_OUTPUT_TOKENS, room)
    return None


def spec_tokens(specs: Sequence[object]) -> int:
    """Rough size of the tool block a request carries."""
    size = 0
    for spec in specs:
        dump = getattr(spec, "model_dump", None)
        size += len(json.dumps(dump() if callable(dump) else str(spec), ensure_ascii=False))
    return size // CHARS_PER_TOKEN
