"""A reviewer blocker gates the sign-off: answered by the agent or dismissed by the operator."""

import asyncio
from pathlib import Path

import pytest
from test_app import _chat_ready_stack

from samurai_ai.agents.provider import ChatResponse, Message, ToolCall, Usage
from samurai_ai.domain.reviewer import ReviewerConfig, ReviewNote, ReviewSeverity
from samurai_ai.tui.app import SamuraiApp

MODEL = "gpt/test-model@v1"


def _answer(text: str = "", calls: tuple[ToolCall, ...] = ()) -> ChatResponse:
    return ChatResponse(
        text=text,
        tool_calls=calls,
        usage=Usage(prompt_tokens=5, completion_tokens=2),
        model_id=MODEL,
        provider_id="gpt",
    )


def _blocker(app: SamuraiApp, operation_id: str, text: str = "the RCE claim has no run") -> str:
    return app.reviewers.record_note(
        operation_id, ReviewNote(ReviewSeverity.BLOCKER, text), text
    )


def test_service_tracks_open_blockers_until_resolved(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    reviewers = SamuraiApp(*stack, operation.id).reviewers
    concern = reviewers.record_note(
        operation.id, ReviewNote(ReviewSeverity.CONCERN, "minor"), "minor"
    )
    blocker = reviewers.record_note(
        operation.id, ReviewNote(ReviewSeverity.BLOCKER, "unsupported"), "unsupported"
    )
    assert [b.id for b in reviewers.open_blockers(operation.id)] == [blocker]
    with pytest.raises(ValueError):
        reviewers.resolve(operation.id, blocker, "  ", actor="agent")
    with pytest.raises(ValueError):
        reviewers.resolve(operation.id, concern, "n/a", actor="agent")
    reviewers.resolve(operation.id, blocker, "run_1 proves it", actor="agent")
    assert reviewers.open_blockers(operation.id) == []
    with pytest.raises(ValueError):
        reviewers.resolve(operation.id, blocker, "again", actor="agent")
    resolved = [
        e.payload for e in stack[0].events(operation.id) if e.event_type == "reviewer.resolved"
    ]
    assert resolved == [{"note_id": blocker, "resolution": "run_1 proves it", "by": "agent"}]


def test_blocker_holds_the_sign_off_until_the_agent_answers(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            note_id = _blocker(app, operation.id)
            sent: list[tuple[tuple[Message, ...], tuple[str, ...]]] = []

            def chat(_operation, _model, messages, **kwargs):  # type: ignore[no-untyped-def]
                sent.append((tuple(messages), tuple(t.name for t in kwargs["tools"])))
                if len(sent) == 1:
                    return _answer("all done, RCE confirmed")
                if len(sent) == 2:
                    call = ToolCall(
                        id="r1",
                        name="resolve_review",
                        arguments={"note_id": note_id, "resolution": "claim withdrawn"},
                    )
                    return _answer(calls=(call,))
                return _answer("done, no RCE claimed")

            app.models.chat = chat  # type: ignore[method-assign]
            await app._chat_loop("wrap up", MODEL, None)

            # past the gate; the turn then meets the usual evidence check
            assert len(sent) >= 3
            resolutions = [
                e for e in stack[0].events(operation.id) if e.event_type == "reviewer.resolved"
            ]
            assert len(resolutions) == 1
            # the tool exists while the blocker is open, and only then
            assert "resolve_review" in sent[0][1]
            assert "resolve_review" not in sent[2][1]
            assert any(note_id in m.content for m in sent[1][0])
            assert app.reviewers.open_blockers(operation.id) == []

    asyncio.run(exercise())


def test_an_ignored_blocker_stops_the_turn(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _blocker(app, operation.id)
            calls = 0

            def chat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                return _answer("done")

            app.models.chat = chat  # type: ignore[method-assign]
            reason = await app._chat_loop("wrap up", MODEL, None)
            assert reason.value == "blocked"
            assert calls == 3

    asyncio.run(exercise())


def test_operator_dismisses_a_blocker_with_a_reason(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            note_id = _blocker(app, operation.id)
            app._handle_reviewer_command(f"dismiss {note_id}")
            assert len(app.reviewers.open_blockers(operation.id)) == 1
            app._handle_reviewer_command(f"dismiss {note_id} false positive, checked by hand")
            assert app.reviewers.open_blockers(operation.id) == []
            by = [
                e.payload["by"]
                for e in stack[0].events(operation.id)
                if e.event_type == "reviewer.resolved"
            ]
            assert by == ["operator"]

    asyncio.run(exercise())


def test_reviewer_sees_store_records_and_its_open_blockers(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            app.reviewers.enable(operation.id, ReviewerConfig(MODEL))
            note_id = _blocker(app, operation.id, "earlier worry")
            requests: list[tuple[Message, ...]] = []

            def chat(_operation, _model, messages, **_kwargs):  # type: ignore[no-untyped-def]
                requests.append(tuple(messages))
                return _answer(
                    '{"notes":[{"severity":"blocker","text":"no evidence for the shell"}]}'
                )

            app.models.chat = chat  # type: ignore[method-assign]
            app._remember(Message(role="assistant", content="I got a shell"))
            await app._drain_reviewer(operation.id)

            prompt = requests[0][-1].content
            assert prompt.startswith("<operation-records>")
            assert "recent tool runs:" in prompt
            assert f"{note_id}: earlier worry" in prompt
            assert "<transcript-delta>" in prompt
            injected = [m for m in app._chat_history if "<reviewer-note" in m.content]
            assert "resolve_review" in injected[-1].content
            assert len(app.reviewers.open_blockers(operation.id)) == 2

    asyncio.run(exercise())
