import asyncio
from pathlib import Path

import pytest
from test_app import _chat_ready_stack

from samurai_ai.agents.provider import ChatResponse, ToolCall, ToolCallDelta, Usage
from samurai_ai.domain.stream_rules import (
    GuardBlockedError,
    RuleMode,
    RuleScope,
    StreamGuardMatcher,
    StreamRule,
)
from samurai_ai.tui.app import SamuraiApp


def _response(call: ToolCall | None = None, text: str = "") -> ChatResponse:
    return ChatResponse(
        text=text,
        tool_calls=(call,) if call else (),
        usage=Usage(prompt_tokens=5, completion_tokens=2),
        model_id="gpt/test-model@v1",
        provider_id="gpt",
    )


def _forbidden_call() -> ToolCall:
    return ToolCall(id="bad", name="shell_exec", arguments={"command": "forbidden"})


def _save_rule(app: SamuraiApp, mode: RuleMode) -> None:
    assert app.operation is not None
    rule = StreamRule(
        name="no-forbidden",
        pattern="forbidden",
        reminder="Do not use the forbidden command",
        scope=RuleScope.ALL,
        mode=mode,
    )
    app.stream_rules.add(app.operation.id, rule)
    app._guard_matcher = StreamGuardMatcher((rule,))


def test_interrupt_retries_without_executing_the_call(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    calls = 0
    stop_seen = False

    async def exercise() -> None:
        nonlocal calls, stop_seen
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _save_rule(app, RuleMode.INTERRUPT)

            def chat(_operation, _model, _messages, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls, stop_seen
                calls += 1
                if calls == 1:
                    kwargs["on_tool_delta"](
                        ToolCallDelta(
                            index=0,
                            id="bad",
                            name="shell_exec",
                            arguments='{"command":"forbidden"}',
                        )
                    )
                    stop_seen = bool(kwargs["should_stop"]())
                    return _response(_forbidden_call())
                return _response(text="compliant")

            app.models.chat = chat  # type: ignore[method-assign]
            response = await app._request_chat_completion(
                operation.id, "gpt/test-model@v1", None, (), guard_rules=True
            )

            assert response is not None and response.text == "compliant"
            assert calls == 2
            assert stop_seen
            assert stack[4].list_runs(operation.id) == []
            assert any("system-interrupt" in message.content for message in app._chat_history)
            assert not any(
                message.role == "assistant" and "forbidden" in message.content
                for message in app._chat_history
            )
            triggered = [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "guard.triggered"
            ]
            assert [event.payload["action"] for event in triggered] == ["retry"]
            assert all(
                set(event.payload) == {"rule", "mode", "source", "action"}
                for event in triggered
            )

    asyncio.run(exercise())


def test_repeated_interrupt_blocks_before_any_tool_run(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _save_rule(app, RuleMode.INTERRUPT)
            calls = 0

            def chat(_operation, _model, _messages, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                kwargs["on_tool_delta"](
                    ToolCallDelta(
                        index=0,
                        id="bad",
                        name="shell_exec",
                        arguments='{"command":"forbidden"}',
                    )
                )
                return _response(_forbidden_call())

            app.models.chat = chat  # type: ignore[method-assign]
            with pytest.raises(GuardBlockedError):
                await app._request_chat_completion(
                    operation.id, "gpt/test-model@v1", None, (), guard_rules=True
                )

            assert calls == 5
            assert stack[4].list_runs(operation.id) == []
            actions = [
                event.payload["action"]
                for event in stack[0].events(operation.id)
                if event.event_type == "guard.triggered"
            ]
            assert actions == ["retry"] * 4 + ["blocked"]

    asyncio.run(exercise())


def test_reminder_follows_the_real_tool_result(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _save_rule(app, RuleMode.REMIND)
            call = ToolCall(
                id="safe", name="shell_exec", arguments={"command": "echo forbidden"}
            )

            def chat(_operation, _model, _messages, **_kwargs):  # type: ignore[no-untyped-def]
                return _response(call)

            app.models.chat = chat  # type: ignore[method-assign]
            response = await app._request_chat_completion(
                operation.id, "gpt/test-model@v1", None, (), guard_rules=True
            )
            assert response is not None
            results = await app._run_round(list(response.tool_calls))

            assert len(stack[4].list_runs(operation.id)) == 1
            assert results[0].content.splitlines()[0] == "[tool result shell_exec exit=0]"
            assert results[0].content.index("[full output in UI") < results[0].content.index(
                "<system-reminder"
            )
            assert "<system-reminder" in results[0].content
            assert "forbidden" in results[0].content
            actions = [
                event.payload["action"]
                for event in stack[0].events(operation.id)
                if event.event_type == "guard.triggered"
            ]
            assert actions == ["remind"]

    asyncio.run(exercise())


def test_text_interrupt_aborts_stream_and_retries(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _save_rule(app, RuleMode.INTERRUPT)
            calls = 0

            def chat(_operation, _model, _messages, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                if calls == 1:
                    kwargs["on_token"]("forbidden")
                    assert kwargs["should_stop"]()
                    return _response(text="forbidden")
                return _response(text="safe answer")

            app.models.chat = chat  # type: ignore[method-assign]
            response = await app._request_chat_completion(
                operation.id, "gpt/test-model@v1", None, (), guard_rules=True
            )

            assert response is not None and response.text == "safe answer"
            assert calls == 2
            sources = [
                event.payload["source"]
                for event in stack[0].events(operation.id)
                if event.event_type == "guard.triggered"
            ]
            assert sources == ["text"]
            assert not any(
                message.role == "assistant" and message.content == "forbidden"
                for message in app._chat_history
            )

    asyncio.run(exercise())


def test_final_response_is_checked_without_stream_deltas(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _save_rule(app, RuleMode.INTERRUPT)
            calls = 0

            def chat(_operation, _model, _messages, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                return _response(_forbidden_call()) if calls == 1 else _response(text="safe")

            app.models.chat = chat  # type: ignore[method-assign]
            response = await app._request_chat_completion(
                operation.id, "gpt/test-model@v1", None, (), guard_rules=True
            )

            assert response is not None and response.text == "safe"
            assert calls == 2
            assert stack[4].list_runs(operation.id) == []

    asyncio.run(exercise())


def test_guard_abort_does_not_stop_an_unguarded_call(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _save_rule(app, RuleMode.INTERRUPT)
            assert app._guard_matcher is not None
            app._guard_matcher.observe_text("forbidden")
            assert app._guard_matcher.abort

            def chat(_operation, _model, _messages, **kwargs):  # type: ignore[no-untyped-def]
                assert not kwargs["should_stop"]()
                return _response(text="auxiliary")

            app.models.chat = chat  # type: ignore[method-assign]
            response = await app._request_chat_completion(
                operation.id, "gpt/test-model@v1", None, ()
            )
            assert response is not None and response.text == "auxiliary"

    asyncio.run(exercise())


def test_guard_command_manages_only_the_current_operation(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            app._handle_guard_command(
                "add no-danger tools interrupt forbidden | choose another command"
            )
            rules = app.stream_rules.rules(operation.id)
            assert len(rules) == 1
            assert rules[0].scope is RuleScope.TOOLS
            assert rules[0].mode is RuleMode.INTERRUPT
            assert rules[0].reminder == "choose another command"

            app._handle_guard_command(
                "add no-danger all remind forbidden | check the command"
            )
            rules = app.stream_rules.rules(operation.id)
            assert len(rules) == 1
            assert rules[0].scope is RuleScope.ALL
            assert rules[0].mode is RuleMode.REMIND
            app._handle_guard_command("rm no-danger")
            assert app.stream_rules.rules(operation.id) == ()

            app._handle_guard_command(
                "add bad tools interrupt a*b* | invalid"
            )
            assert app.stream_rules.rules(operation.id) == ()
            app._chat_busy = True
            app._handle_guard_command(
                "add blocked tools interrupt forbidden | not saved"
            )
            assert app.stream_rules.rules(operation.id) == ()

    asyncio.run(exercise())


def test_chat_loop_reports_blocked_after_guard_retries(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            _save_rule(app, RuleMode.INTERRUPT)

            def chat(_operation, _model, _messages, **kwargs):  # type: ignore[no-untyped-def]
                kwargs["on_tool_delta"](
                    ToolCallDelta(
                        index=0,
                        id="bad",
                        name="shell_exec",
                        arguments='{"command":"forbidden"}',
                    )
                )
                return _response(_forbidden_call())

            app.models.chat = chat  # type: ignore[method-assign]
            reason = await app._chat_loop(
                "run the check", "gpt/test-model@v1", None
            )

            assert reason.value == "blocked"
            assert stack[4].list_runs(operation.id) == []
            actions = [
                event.payload["action"]
                for event in stack[0].events(operation.id)
                if event.event_type == "guard.triggered"
            ]
            assert actions == ["retry"] * 4 + ["blocked"]

    asyncio.run(exercise())
