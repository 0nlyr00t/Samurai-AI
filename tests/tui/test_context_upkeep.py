"""The chat loop keeps its window: elide, fold into a brief, recover from overflow."""

import asyncio
from pathlib import Path

from test_app import _chat_ready_stack

from samurai_ai.agents.context import COMPACTED_MARK
from samurai_ai.agents.provider import (
    ChatResponse,
    Message,
    ToolCall,
    Usage,
    repair_tool_pairs,
    tool_result,
)
from samurai_ai.domain.models import ModelError
from samurai_ai.tui.app import SamuraiApp

MODEL = "gpt/test-model@v1"


def _text(text: str) -> ChatResponse:
    return ChatResponse(
        text=text,
        usage=Usage(prompt_tokens=5, completion_tokens=2),
        model_id=MODEL,
        provider_id="gpt",
    )


def _fill(app: SamuraiApp, rounds: int) -> None:
    app._chat_history = [
        Message(role="system", content="sys"),
        Message(role="user", content="start"),
    ]
    for index in range(rounds):
        call = ToolCall(id=f"old{index}", name="shell_exec", arguments={"command": f"c{index}"})
        app._chat_history += [
            Message(role="assistant", content="", tool_calls=(call,)),
            tool_result(call, "[tool result shell_exec exit=0]\n" + "y" * 1500),
        ]


def _compact_events(stack, operation_id):  # type: ignore[no-untyped-def]
    return [
        event.payload
        for event in stack[0].events(operation_id)
        if event.event_type == "chat.compact"
    ]


def test_full_window_folds_into_a_brief_with_a_recollection(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    # small enough that eliding old output alone cannot make the history fit
    stack[2].set_value(f"model.context_limit.{MODEL}", "600")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _fill(app, 12)
            app._compact_narrative = True
            seen: list[tuple[Message, ...]] = []

            def chat(_operation, _model, messages, **kwargs):  # type: ignore[no-untyped-def]
                seen.append(tuple(messages))
                assert kwargs["tools"] == ()
                return _text("we ran c0..c9, nothing open")

            app.models.chat = chat  # type: ignore[method-assign]
            folded = await app._maintain_context(operation.id)

            assert folded
            assert len(seen) == 1
            brief = app._chat_history[1]
            assert brief.content.startswith(COMPACTED_MARK)
            assert "we ran c0..c9" in brief.content
            assert '"command": "c0"' in brief.content
            history = tuple(app._chat_history)
            assert repair_tool_pairs(history) == history
            assert _compact_events(stack, operation.id)[-1]["reason"] == "auto"
            assert _compact_events(stack, operation.id)[-1]["narrative"] is True

    asyncio.run(exercise())


def test_headless_fold_stays_mechanical_and_small_windows_are_left_alone(
    tmp_path: Path,
) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    stack[2].set_value(f"model.context_limit.{MODEL}", "200000")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _fill(app, 12)

            def chat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                raise AssertionError("no model call expected")

            app.models.chat = chat  # type: ignore[method-assign]
            assert not await app._maintain_context(operation.id)
            assert len(app._chat_history) == 26
            # forced (an overflow refusal): folds without asking a model
            assert await app._maintain_context(operation.id, force=True)
            assert app._chat_history[1].content.startswith(COMPACTED_MARK)
            assert "Recollection" not in app._chat_history[1].content

    asyncio.run(exercise())


def test_overflow_refusal_folds_and_retries_once(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _fill(app, 12)
            calls = 0

            def chat(_operation, _model, _messages, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ModelError("prompt is too long: 210000 tokens > 200000 maximum")
                return _text("done")

            app.models.chat = chat  # type: ignore[method-assign]
            reason = await app._chat_loop("continue", MODEL, None)

            assert reason.value != "model-error"
            assert calls >= 2
            assert _compact_events(stack, operation.id)[-1]["reason"] == "force"

    asyncio.run(exercise())


def test_second_overflow_surfaces_as_an_error(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _fill(app, 12)

            def chat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                raise ModelError("context_length_exceeded")

            app.models.chat = chat  # type: ignore[method-assign]
            reason = await app._chat_loop("continue", MODEL, None)
            assert reason.value == "model-error"
            assert len(_compact_events(stack, operation.id)) == 1

    asyncio.run(exercise())


def test_each_agent_round_ends_with_a_fresh_unstored_snapshot(tmp_path: Path) -> None:
    from samurai_ai.domain.kernel import PlanTask

    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            sent: list[tuple[Message, ...]] = []
            call = ToolCall(id="c1", name="update_todo", arguments={"items": []})

            def chat(_operation, _model, messages, **_kwargs):  # type: ignore[no-untyped-def]
                sent.append(tuple(messages))
                if len(sent) == 1:
                    # between rounds the plan changes under the model
                    stack[1].create_plan(
                        operation.id,
                        "Web map",
                        [PlanTask(id="map", title="Map the login flow")],
                        [],
                    )
                    return ChatResponse(
                        text="",
                        tool_calls=(call,),
                        usage=Usage(prompt_tokens=5, completion_tokens=2),
                        model_id=MODEL,
                        provider_id="gpt",
                    )
                return _text("done")

            app.models.chat = chat  # type: ignore[method-assign]
            await app._chat_loop("map it", MODEL, None)

            first, second = sent[0], sent[1]
            assert first[-1].content.startswith("<operation-state>")
            assert "Map the login flow" not in first[-1].content
            assert "Map the login flow" in second[-1].content
            assert "<operation-state>" not in first[0].content
            assert not any(
                message.content.startswith("<operation-state>")
                for message in app._chat_history
            )

    asyncio.run(exercise())


def test_a_call_cut_by_the_output_cap_is_not_run(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            sent: list[tuple[Message, ...]] = []
            cut = ToolCall(id="c1", name="shell_exec", arguments={"command": "cat > x <<EOF"})

            def chat(_operation, _model, messages, **_kwargs):  # type: ignore[no-untyped-def]
                sent.append(tuple(messages))
                if len(sent) == 1:
                    return ChatResponse(
                        text="writing the script",
                        tool_calls=(cut,),
                        usage=Usage(prompt_tokens=5, completion_tokens=2),
                        model_id=MODEL,
                        provider_id="gpt",
                        stop_reason="length",
                    )
                return _text("done")

            app.models.chat = chat  # type: ignore[method-assign]
            await app._chat_loop("write it", MODEL, None)

            assert len(sent) == 2
            assert stack[4].list_runs(operation.id) == []
            assert not any(message.tool_calls for message in app._chat_history)
            assert any("output token limit" in m.content for m in sent[1])

    asyncio.run(exercise())


def test_repeated_cut_calls_stop_the_turn(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            calls = 0
            cut = ToolCall(id="c1", name="shell_exec", arguments={"command": "x"})

            def chat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                return ChatResponse(
                    text="",
                    tool_calls=(cut,),
                    usage=Usage(prompt_tokens=5, completion_tokens=2),
                    model_id=MODEL,
                    provider_id="gpt",
                    stop_reason="max_tokens",
                )

            app.models.chat = chat  # type: ignore[method-assign]
            reason = await app._chat_loop("write it", MODEL, None)
            assert reason.value == "model-error"
            assert calls == 3
            assert stack[4].list_runs(operation.id) == []

    asyncio.run(exercise())
