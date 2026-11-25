import pytest

from samurai_ai.agents.provider import ToolCall, ToolCallDelta
from samurai_ai.domain.stream_rules import (
    RuleMode,
    RuleScope,
    StreamGuardMatcher,
    StreamRule,
    StreamRuleError,
)


def _rule(
    name: str = "no-danger",
    *,
    scope: RuleScope = RuleScope.ALL,
    mode: RuleMode = RuleMode.INTERRUPT,
    pattern: str = "forbidden",
) -> StreamRule:
    return StreamRule(
        name=name,
        pattern=pattern,
        reminder="Use a different approach",
        scope=scope,
        mode=mode,
    )


@pytest.mark.parametrize(
    "pattern",
    ["(?=forbidden)", "\\1", "(?:ab)+", "(a|b)*", "[z-a]", "a*b*", ".*x.*"],
)
def test_rule_rejects_unbounded_or_invalid_regex(pattern: str) -> None:
    with pytest.raises(StreamRuleError):
        _rule(pattern=pattern)


def test_interrupt_matches_incremental_tool_arguments_once() -> None:
    matcher = StreamGuardMatcher((_rule(),))
    matcher.observe_tool(ToolCallDelta(index=0, name="shell_exec", arguments='{"command":"id'))
    assert not matcher.abort
    matcher.observe_tool(
        ToolCallDelta(index=0, name="shell_exec", arguments='{"command":"forbidden"}')
    )
    assert matcher.abort

    evaluation = matcher.finalize(
        "",
        (
            ToolCall(
                id="call-1",
                name="shell_exec",
                arguments={"command": "forbidden"},
            ),
        ),
    )
    assert [(match.rule.name, match.source) for match in evaluation.interrupts] == [
        ("no-danger", "tools")
    ]


def test_reminder_is_scoped_and_fires_once_per_turn() -> None:
    matcher = StreamGuardMatcher(
        (_rule(scope=RuleScope.TOOLS, mode=RuleMode.REMIND),)
    )
    first = matcher.finalize(
        "forbidden in prose only",
        (
            ToolCall(id="one", name="shell_exec", arguments={"command": "forbidden"}),
            ToolCall(id="two", name="shell_exec", arguments={"command": "forbidden"}),
        ),
    )
    assert not first.interrupts
    assert len(first.reminders) == 1
    assert first.reminders[0].call_id == "one"
    matcher.mark_reminded(first.reminders)

    matcher.begin_attempt()
    second = matcher.finalize(
        "",
        (ToolCall(id="three", name="shell_exec", arguments={"command": "forbidden"}),),
    )
    assert not second.reminders


def test_stream_reset_drops_partial_but_preserves_reminder_history() -> None:
    matcher = StreamGuardMatcher(
        (_rule(scope=RuleScope.TEXT, mode=RuleMode.REMIND),)
    )
    matcher.observe_text("forbidden")
    matcher.reset_stream()
    assert not matcher.abort
    assert not matcher.finalize("safe", ()).interrupts

    accepted = matcher.finalize("forbidden", ())
    matcher.mark_reminded(accepted.reminders)
    matcher.reset_stream()
    assert not matcher.finalize("forbidden", ()).reminders
