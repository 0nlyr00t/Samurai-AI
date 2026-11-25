import asyncio
import time
from pathlib import Path
from unittest.mock import patch

from test_app import _chat_ready_stack

from samurai_ai.agents.provider import ChatResponse, Message, Usage
from samurai_ai.domain.models import ProviderUnavailableError
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.reviewer import ReviewerConfig
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.tui.app import SamuraiApp
from samurai_ai.tui.chat import _ReviewerRuntime
from samurai_ai.tui.journal import BlockJournal


def _review_response(text: str, model_id: str = "gpt/test-model@v1") -> ChatResponse:
    return ChatResponse(
        text=text,
        usage=Usage(prompt_tokens=10, completion_tokens=4),
        model_id=model_id,
        provider_id="gpt",
    )


def test_review_batch_redacts_vault_and_does_not_review_itself(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    stack[-1].add_model("gpt/fast")
    stack[5].add("target-password", "s3cr3t-value", operation.id)
    requests: list[tuple[tuple[Message, ...], dict[str, object]]] = []

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            app._remember(Message(role="user", content="old context before opt-in"))
            app.reviewers.enable(
                operation.id, ReviewerConfig("gpt/test-model@v1", "gpt/fast@v1")
            )

            def chat(_operation, _model, messages, **kwargs):  # type: ignore[no-untyped-def]
                requests.append((messages, kwargs))
                return _review_response(
                    '{"notes":[{"severity":"concern","text":"Check the evidence"}]}'
                )

            app.models.chat = chat  # type: ignore[method-assign]
            app._remember(Message(role="user", content="password is s3cr3t-value"))
            await app._drain_reviewer(operation.id)
            await app._drain_reviewer(operation.id)

            assert len(requests) == 1
            messages, kwargs = requests[0]
            outgoing = messages[-1].content
            assert "s3cr3t-value" not in outgoing
            assert "old context before opt-in" not in outgoing
            assert "***REDACTED***" in outgoing
            assert kwargs["fallbacks"] == ("gpt/fast@v1",)
            assert kwargs["extra_payload"] == {"reviewer": True}
            assert "on_token" not in kwargs
            notes = [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "reviewer.note"
            ]
            assert len(notes) == 1
            assert "s3cr3t-value" not in notes[0].payload["text"]
            assert sum(
                message.content.startswith("<reviewer-note")
                for message in app._chat_history
            ) == 1

    asyncio.run(exercise())


def test_blocker_is_a_notice_not_a_terminal_decision(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            config = app.reviewers.config(operation.id)
            assert config is None
            config = app.reviewers.enable(
                operation.id, ReviewerConfig("gpt/test-model@v1")
            )
            app.models.chat = lambda *_args, **_kwargs: _review_response(  # type: ignore[method-assign]
                '{"notes":[{"severity":"blocker","text":"Pause and inspect"}]}'
            )
            app._remember(Message(role="assistant", content="The target is owned"))
            await app._drain_reviewer(operation.id)

            assert app._last_turn_reason is None
            assert "Pause and inspect" in app.query_one("#journal", BlockJournal).text()
            assert any(
                message.content.startswith("<reviewer-note")
                for message in app._chat_history
            )

    asyncio.run(exercise())


def test_review_error_does_not_escape_into_the_turn(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            app.reviewers.enable(operation.id, ReviewerConfig("gpt/test-model@v1"))

            def chat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError("review unavailable")

            app.models.chat = chat  # type: ignore[method-assign]
            app._remember(Message(role="user", content="new work"))
            await app._drain_reviewer(operation.id)

            errors = [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "reviewer.error"
            ]
            assert errors[-1].payload["reason"] == "review unavailable"
            assert app._last_turn_reason is None

    asyncio.run(exercise())


def test_note_is_written_to_its_original_operation(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        other = stack[0].create(
            "Other", OperationMode.BUG_BOUNTY, ScopeContract(includes=())
        )
        async with app.run_test(size=(120, 30)):
            config = ReviewerConfig("gpt/test-model@v1")
            app.operation = other
            app.models.chat = lambda *_args, **_kwargs: _review_response(  # type: ignore[method-assign]
                '{"notes":[{"severity":"info","text":"For the original operation"}]}'
            )
            await app._review_batch(
                operation.id, config, [Message(role="user", content="delta")]
            )

            assert not app._chat_history
            restored, _total, _truncated = app.chat_history.tail(operation.id)
            assert any(
                message.content.startswith("<reviewer-note") for message in restored
            )

    asyncio.run(exercise())


def test_reviewer_drain_is_bounded(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            app.reviewers.enable(operation.id, ReviewerConfig("gpt/test-model@v1"))

            def chat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                time.sleep(0.2)
                return _review_response('{"notes":[]}')

            app.models.chat = chat  # type: ignore[method-assign]
            app._remember(Message(role="user", content="new work"))
            with patch("samurai_ai.tui.chat._REVIEWER_DRAIN_SECONDS", 0.01):
                await app._drain_reviewer(operation.id)

            errors = [
                event.payload["reason"]
                for event in stack[0].events(operation.id)
                if event.event_type == "reviewer.error"
            ]
            assert "reviewer drain timeout" in errors

    asyncio.run(exercise())


def test_transient_primary_failure_uses_the_explicit_fallback(tmp_path: Path) -> None:
    class _Provider:
        async def complete(self, request):  # type: ignore[no-untyped-def]
            if request.model_id == "gpt/test-model@v1":
                raise ProviderUnavailableError("rate limited", status=429, transient=True)
            return _review_response(
                '{"notes":[{"severity":"info","text":"Fallback reviewed"}]}',
                model_id=request.model_id,
            )

    provider = _Provider()
    stack, operation = _chat_ready_stack(tmp_path, provider)
    stack[-1].add_model("gpt/fast")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            config = ReviewerConfig("gpt/test-model@v1", "gpt/fast@v1")
            await app._review_batch(
                operation.id, config, [Message(role="user", content="new delta")]
            )

            fallbacks = [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "model.fallback"
            ]
            assert fallbacks[-1].payload["to_model"] == "gpt/fast@v1"
            notes = [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "reviewer.note"
            ]
            assert notes[-1].payload["text"] == "Fallback reviewed"
            calls = [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "model.call"
            ]
            assert calls[-1].payload["reviewer"] is True
            assert calls[-1].payload["model_id"] == "gpt/fast@v1"

    asyncio.run(exercise())


def test_reviewer_command_controls_opt_in_and_busy_state(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    stack[-1].add_model("gpt/fast")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            app._handle_reviewer_command(
                "on gpt/test-model@v1 gpt/fast@v1"
            )
            assert app.reviewers.config(operation.id) == ReviewerConfig(
                "gpt/test-model@v1", "gpt/fast@v1"
            )
            app._chat_busy = True
            app._handle_reviewer_command("off")
            assert app.reviewers.config(operation.id) is not None
            app._chat_busy = False
            app._handle_reviewer_command("off")
            assert app.reviewers.config(operation.id) is None

    asyncio.run(exercise())


def test_reviewer_batch_is_bounded() -> None:
    runtime = _ReviewerRuntime(
        config=ReviewerConfig("gpt/test-model@v1"),
        queue=[Message(role="user", content=str(index)) for index in range(45)],
    )
    batch = SamuraiApp._take_reviewer_batch(runtime)
    assert len(batch) == 40
    assert len(runtime.queue) == 5

    runtime.queue = [
        Message(role="user", content="x" * 20_000),
        Message(role="user", content="y" * 20_000),
    ]
    batch = SamuraiApp._take_reviewer_batch(runtime)
    assert len(batch) == 1
    assert len(batch[0].content) <= 24_001
    assert len(runtime.queue) == 1
