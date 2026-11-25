import asyncio
from pathlib import Path

from test_app import _chat_ready_stack
from textual.widgets import Input

from samurai_ai.agents.provider import ChatResponse, ToolCall, Usage
from samurai_ai.tui.app import SamuraiApp
from samurai_ai.tui.journal import BlockJournal


class _PlanningProvider:
    def __init__(self, *, checklist: bool) -> None:
        self.checklist = checklist
        self.round = 0
        self.tool_models: list[str] = []

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model", "fast"]

    async def complete(self, request):  # type: ignore[no-untyped-def]
        usage = Usage(prompt_tokens=3, completion_tokens=2)
        if not request.tools:
            return ChatResponse(
                text="stop",
                usage=usage,
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=1,
            )
        self.round += 1
        self.tool_models.append(request.model_id)
        if self.round == 1 and self.checklist:
            call = ToolCall(
                id="todo",
                name="update_todo",
                arguments={
                    "items": [
                        {"title": "Map the service", "status": "done"},
                        {"title": "Validate the result", "status": "in_progress"},
                    ]
                },
            )
        elif self.round == (2 if self.checklist else 1):
            call = ToolCall(
                id="shell", name="shell_exec", arguments={"command": "true"}
            )
        else:
            return ChatResponse(
                text="done",
                usage=usage,
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=1,
            )
        return ChatResponse(
            text="",
            tool_calls=(call,),
            usage=usage,
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=1,
        )


def _configure_execution(stack) -> None:  # type: ignore[no-untyped-def]
    stack[-1].add_model("gpt/fast")
    stack[2].set_value("model.execution", "gpt/fast@v1")


def test_completed_shell_hands_off_after_a_checklist(tmp_path: Path) -> None:
    provider = _PlanningProvider(checklist=True)
    stack, operation = _chat_ready_stack(tmp_path, provider)
    _configure_execution(stack)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one("#command-input", Input).value = "validate the lab"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert provider.tool_models == [
                "gpt/test-model@v1",
                "gpt/test-model@v1",
                "gpt/fast@v1",
            ]
            assert app._active_chat_model() == "gpt/fast@v1"
            events = [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "model.handoff"
            ]
            assert len(events) == 1
            assert events[0].payload == {
                "from": "gpt/test-model@v1",
                "to": "gpt/fast@v1",
                "reason": "shell_observed",
            }
            assert any(
                "Follow the existing checklist" in str(message.content)
                for message in app._chat_history
            )
            assert "execution model continues" in app.query_one("#journal", BlockJournal).text()

        restored = SamuraiApp(*stack, operation.id)
        async with restored.run_test(size=(120, 30)):
            assert restored._active_chat_model() == "gpt/fast@v1"
            restored._handle_handoff_command("restart")
            assert restored._active_chat_model() == "gpt/test-model@v1"
            stack[-1].add_model("gpt/other")
            restored._on_model_chosen(("gpt/other@v1", "low"))
            assert restored._active_chat_model() == "gpt/other@v1"
            assert restored.model_routing.execution_model() == "gpt/fast@v1"
            assert stack[0].events(operation.id)[-1].payload == {
                "reason": "model_selected"
            }
            restored._handle_handoff_command("off")
            assert restored.model_routing.execution_model() is None

    asyncio.run(exercise())


def test_shell_without_a_checklist_keeps_the_planning_model(tmp_path: Path) -> None:
    provider = _PlanningProvider(checklist=False)
    stack, operation = _chat_ready_stack(tmp_path, provider)
    _configure_execution(stack)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one("#command-input", Input).value = "run one check"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert provider.tool_models == ["gpt/test-model@v1", "gpt/test-model@v1"]
            assert app._active_chat_model() == "gpt/test-model@v1"
            assert not [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "model.handoff"
            ]

    asyncio.run(exercise())
