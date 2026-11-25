"""Operator-defined rules over assistant text and tool-call streams."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from samurai_ai.agents.provider import ToolCall, ToolCallDelta

MAX_RULES = 32
MAX_PATTERN_CHARS = 512
MAX_REMINDER_CHARS = 2000
MAX_BUFFER_CHARS = 16_384


class StreamRuleError(ValueError):
    pass


class GuardBlockedError(StreamRuleError):
    """The same interrupting rule survived the bounded retry budget."""


class RuleScope(StrEnum):
    TEXT = "text"
    TOOLS = "tools"
    ALL = "all"


class RuleMode(StrEnum):
    REMIND = "remind"
    INTERRUPT = "interrupt"


@dataclass(frozen=True)
class StreamRule:
    name: str
    pattern: str
    reminder: str
    scope: RuleScope
    mode: RuleMode

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.name):
            raise StreamRuleError("rule name must be 1-64 lower-case letters, digits, _ or -")
        if not 1 <= len(self.pattern) <= MAX_PATTERN_CHARS:
            raise StreamRuleError(f"rule pattern must be 1-{MAX_PATTERN_CHARS} characters")
        if not 1 <= len(self.reminder.strip()) <= MAX_REMINDER_CHARS:
            raise StreamRuleError(f"reminder must be 1-{MAX_REMINDER_CHARS} characters")
        _validate_pattern(self.pattern)

    def payload(self) -> dict[str, str]:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "reminder": self.reminder.strip(),
            "scope": self.scope.value,
            "mode": self.mode.value,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StreamRule:
        try:
            return cls(
                name=str(payload["name"]),
                pattern=str(payload["pattern"]),
                reminder=str(payload["reminder"]),
                scope=RuleScope(str(payload["scope"])),
                mode=RuleMode(str(payload["mode"])),
            )
        except (KeyError, ValueError) as exc:
            raise StreamRuleError(f"invalid stored stream rule: {exc}") from exc


@dataclass(frozen=True)
class RuleMatch:
    rule: StreamRule
    source: Literal["text", "tools"]
    call_id: str | None = None


@dataclass(frozen=True)
class GuardEvaluation:
    interrupts: tuple[RuleMatch, ...] = ()
    reminders: tuple[RuleMatch, ...] = ()


def _validate_pattern(pattern: str) -> None:
    """Reject constructs that can backtrack without a useful bound."""
    if re.search(r"\\[1-9]|\\g<|\(\?(?:[=!]|<[=!]|\()", pattern):
        raise StreamRuleError("lookaround and backreferences are not allowed")
    if re.search(r"\)(?:[*+?]|\{)", pattern):
        raise StreamRuleError("quantified groups are not allowed")
    unbounded = 0
    escaped = False
    in_class = False
    for char in pattern:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char in "*+" and not in_class:
            unbounded += 1
    if unbounded > 1:
        raise StreamRuleError("at most one unbounded quantifier is allowed")
    for match in re.finditer(r"\{(\d+)(?:,(\d*)?)?\}", pattern):
        lower = int(match.group(1))
        upper = int(match.group(2)) if match.group(2) else lower
        if max(lower, upper) > 1000:
            raise StreamRuleError("regex repetition bounds must not exceed 1000")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise StreamRuleError(f"invalid regex: {exc}") from exc


class StreamGuardMatcher:
    """Incremental interrupt checks plus an exact finalized-response check."""

    def __init__(self, rules: tuple[StreamRule, ...]) -> None:
        self._rules = rules
        self._compiled = {rule.name: re.compile(rule.pattern) for rule in rules}
        self._reminded: set[str] = set()
        self._interrupting = tuple(rule for rule in rules if rule.mode is RuleMode.INTERRUPT)
        self.begin_attempt()

    @property
    def abort(self) -> bool:
        return self._abort

    def begin_attempt(self) -> None:
        self._text = ""
        self._text_scanned = 0
        self._tools: dict[int, str] = {}
        self._abort = False
        self._attempt_interrupts: list[RuleMatch] = []

    def reset_stream(self) -> None:
        """Drop a provider's abandoned partial response, not turn-level memory."""
        self.begin_attempt()

    def observe_text(self, delta: str) -> None:
        if self._abort or not delta:
            return
        self._text = (self._text + delta)[-MAX_BUFFER_CHARS:]
        scan_due = len(self._text) < 4096 or len(self._text) >= max(
            4096, int(self._text_scanned * 1.25)
        )
        if scan_due:
            self._text_scanned = len(self._text)
            self._observe_interrupts("text", self._text)

    def observe_tool(self, delta: ToolCallDelta) -> None:
        if self._abort:
            return
        self._tools[delta.index] = delta.arguments[-MAX_BUFFER_CHARS:]
        self._observe_interrupts("tools", self._tools[delta.index])

    def finalize(self, text: str, calls: tuple[ToolCall, ...]) -> GuardEvaluation:
        interrupts = list(self._attempt_interrupts)
        reminders: list[RuleMatch] = []
        candidates = [
            RuleMatch(rule, "text") for rule in self._matching(text or self._text, "text")
        ]
        for call in calls:
            raw = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
            candidates.extend(
                RuleMatch(rule, "tools", call.id)
                for rule in self._matching(raw, "tools")
            )
        interrupt_keys = {(match.rule.name, match.source) for match in interrupts}
        reminder_names = set(self._reminded)
        for match in candidates:
            if match.rule.mode is RuleMode.INTERRUPT:
                key = (match.rule.name, match.source)
                if key in interrupt_keys:
                    continue
                interrupt_keys.add(key)
                interrupts.append(match)
            elif match.rule.name not in reminder_names:
                reminder_names.add(match.rule.name)
                reminders.append(match)
        return GuardEvaluation(_dedupe(interrupts), tuple(reminders))

    def mark_reminded(self, matches: tuple[RuleMatch, ...]) -> None:
        self._reminded.update(match.rule.name for match in matches)

    def _observe_interrupts(self, source: Literal["text", "tools"], value: str) -> None:
        for rule in self._interrupting:
            if not _scope_matches(rule.scope, source):
                continue
            if self._compiled[rule.name].search(value):
                self._attempt_interrupts.append(RuleMatch(rule, source))
                self._abort = True
                return

    def _matching(
        self, value: str, source: Literal["text", "tools"]
    ) -> tuple[StreamRule, ...]:
        if not value:
            return ()
        return tuple(
            rule
            for rule in self._rules
            if _scope_matches(rule.scope, source) and self._compiled[rule.name].search(value)
        )


def _scope_matches(scope: RuleScope, source: Literal["text", "tools"]) -> bool:
    return scope is RuleScope.ALL or scope.value == source


def _dedupe(matches: list[RuleMatch]) -> tuple[RuleMatch, ...]:
    unique: list[RuleMatch] = []
    seen: set[tuple[str, str, str | None]] = set()
    for match in matches:
        key = (match.rule.name, match.source, match.call_id)
        if key not in seen:
            seen.add(key)
            unique.append(match)
    return tuple(unique)
