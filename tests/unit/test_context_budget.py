"""Context upkeep: elide old tool output, fold old history, never split a pair."""

from __future__ import annotations

from samurai_ai.agents.context import (
    BRIEF_LIMIT,
    COMPACTED_MARK,
    ELIDED_MARK,
    FALLBACK_CONTEXT_TOKENS,
    ContextBudget,
    attempted_calls,
    build_brief,
    compaction_cut,
    elide_tool_outputs,
    estimate_tokens,
)
from samurai_ai.agents.provider import Message, ToolCall, repair_tool_pairs, tool_result


def _round(index: int, body: int = 2000) -> list[Message]:
    call = ToolCall(id=f"c{index}", name="shell_exec", arguments={"command": f"cmd {index}"})
    return [
        Message(role="assistant", content="", tool_calls=(call,)),
        tool_result(call, f"[tool result shell_exec exit=0]\n{'x' * body}"),
    ]


def _history(rounds: int) -> list[Message]:
    out = [Message(role="system", content="sys"), Message(role="user", content="go")]
    for index in range(rounds):
        out += _round(index)
    return out


def test_budget_falls_back_when_the_window_is_unknown() -> None:
    assert ContextBudget.for_limit(0).limit == FALLBACK_CONTEXT_TOKENS
    budget = ContextBudget.for_limit(10_000)
    assert (budget.elide_at, budget.compact_at, budget.keep) == (5_000, 7_000, 2_500)


def test_elision_keeps_the_protected_tail_and_every_pair() -> None:
    history = _history(6)
    elided, saved = elide_tool_outputs(history, protect_tokens=1_200)
    assert saved > 0
    assert estimate_tokens(elided) < estimate_tokens(history)
    # the newest result is protected, the oldest is cut to its header
    assert ELIDED_MARK not in elided[-1].content
    assert elided[3].content.startswith("[tool result shell_exec exit=0]\n" + ELIDED_MARK)
    assert [m.role for m in elided] == [m.role for m in history]
    assert [m.tool_call_id for m in elided] == [m.tool_call_id for m in history]
    # idempotent: an elided result is not elided again
    again, more = elide_tool_outputs(elided, protect_tokens=1_200)
    assert more == 0 and again == elided


def test_cut_never_opens_on_a_result() -> None:
    history = _history(6)
    for keep in range(0, 4_000, 50):
        cut = compaction_cut(history, keep)
        assert cut >= 1
        assert history[cut].role != "tool" if cut < len(history) else True
        kept = [history[0], *history[cut:]]
        assert repair_tool_pairs(kept) == tuple(kept)


def test_cut_returns_one_when_everything_fits() -> None:
    assert compaction_cut(_history(1), keep_tokens=100_000) == 1


def test_brief_leads_with_records_and_lists_calls() -> None:
    folded = _history(3)[1:]
    brief = build_brief(folded, "- fact f1: port 22 open", "we scanned ports")
    assert brief.role == "user"
    text = brief.content
    assert text.startswith(COMPACTED_MARK)
    assert text.index("fact f1") < text.index("cmd 0") < text.index("we scanned ports")
    assert "(unverified)" in text


def test_brief_is_bounded_and_survives_without_narrative() -> None:
    brief = build_brief(_history(2)[1:], "x" * (BRIEF_LIMIT * 2), None)
    assert len(brief.content) <= BRIEF_LIMIT
    assert "Recollection" not in brief.content


def test_attempted_calls_dedupe_and_bound() -> None:
    messages: list[Message] = []
    for index in range(40):
        messages += _round(index % 30, body=1)
    lines = attempted_calls(messages, limit=10)
    assert len(lines) == 11
    assert lines[5].startswith("- … 20 more")
