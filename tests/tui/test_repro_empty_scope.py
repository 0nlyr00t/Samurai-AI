"""Regression coverage for the empty TARGET/SCOPE/TASKS dead end.

The scenarios originally reproduced a task losing its URL, an empty scope
declining its own target forever, a missing initial plan, and a refusal being
reported as a completed turn. They now pin the repaired behavior without
network access or a real model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_app import _minimal_stack

from samurai_ai.domain.execution import ToolScopeError
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.permission import PermissionMode
from samurai_ai.domain.scope import ScopeContract

#: ordinary engagement targets, including domains the old extractor missed
URL_TARGETS = (
    "исследуй веб-ресурс https://example.co.uk",
    "проверь https://target.dev",
    "research https://demo-site.app",
    "исследуй https://portal.example.ai/app",
    "изучи http://10.10.11.5:8080",
)


def _chat_stack(tmp_path: Path, provider: object) -> tuple:
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    vault = stack[5]
    models = stack[-1]
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    settings.set_value("tui.permission_mode", "yolo")
    return stack


def test_task_targets_cover_common_tlds(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("ru")
    operation = stack[0].create(
        "Repro web", OperationMode.BUG_BOUNTY, ScopeContract(includes=())
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import SamuraiApp

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            seen: dict[str, tuple[str, ...]] = {}
            for text in URL_TARGETS:
                app.operation = app.service.replace_scope(
                    operation.id, ScopeContract(includes=())
                )
                app._chat_history.clear()
                app._scope_offered.clear()
                await app._maybe_offer_scope(text)
                seen[text] = app.service.get(operation.id).scope.includes
            assert seen == {
                "исследуй веб-ресурс https://example.co.uk": ("example.co.uk",),
                "проверь https://target.dev": ("target.dev",),
                "research https://demo-site.app": ("demo-site.app",),
                "исследуй https://portal.example.ai/app": ("portal.example.ai",),
                "изучи http://10.10.11.5:8080": ("10.10.11.5",),
            }
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_empty_scope_accepts_goal_named_target(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Repro refusal", OperationMode.BUG_BOUNTY, ScopeContract(includes=())
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import SamuraiApp

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            app.operation = app.service.set_goal(
                operation.id, "research https://example.co.uk", automatic=True
            )

            async def include(_target: str, _reasons: list[str]) -> tuple[bool, str]:
                return True, "named in the operation goal"

            app._scope_analyst = include  # type: ignore[method-assign]
            verdict, why = await app._decide_scope(
                "example.co.uk", ToolScopeError("outside"), PermissionMode.ASK
            )
            assert verdict is True
            assert why == "named in the operation goal"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_scope_change_invalidates_cached_refusal(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Repro cache", OperationMode.BUG_BOUNTY, ScopeContract(includes=())
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import SamuraiApp

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            allowed_key = (operation.id, "https://example.co.uk")
            unrelated_key = (operation.id, "unrelated.dev")
            manual_key = (operation.id, "target.dev")
            app._scope_declined.update((allowed_key, unrelated_key, manual_key))
            app._extend_scope(["example.co.uk"])
            assert allowed_key not in app._scope_declined
            assert unrelated_key in app._scope_declined
            app._scope_edit(add=["target.dev"])
            assert manual_key not in app._scope_declined
            assert unrelated_key in app._scope_declined
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


class _NarratingProvider:
    """Answers, never calls a tool — one useless turn."""

    def __init__(self) -> None:
        self.calls = 0

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, Usage

        self.calls += 1
        if request.max_tokens is not None and request.max_tokens <= 8:
            return ChatResponse(
                text="continue",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=1,
            )
        return ChatResponse(
            text="no target in scope, add one by hand",
            usage=Usage(prompt_tokens=3, completion_tokens=2),
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=5,
        )


def test_empty_plan_keeps_propose_plan_visible_and_nudges_once(tmp_path: Path) -> None:
    from samurai_ai.domain.kernel import PlanTask

    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Plan bootstrap", OperationMode.BUG_BOUNTY, ScopeContract(includes=("target.dev",))
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import SamuraiApp

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            spec = next(item for item in app._tool_specs() if item.name == "propose_plan")
            assert "operator sees the proposal" in spec.description

            app._plan_nudged = False
            app._maybe_nudge_initial_plan()
            app._maybe_nudge_initial_plan()
            assert len(app._steer_queue) == 1
            assert "propose_plan" in app._steer_queue[0].text

            stack[1].create_plan(
                operation.id,
                "Web map",
                [PlanTask(id="map", title="Map the application")],
                [],
            )
            spec = next(item for item in app._tool_specs() if item.name == "propose_plan")
            assert spec.description == "Propose a plan of tasks for the operation."
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_system_prompt_exposes_operation_state(tmp_path: Path) -> None:
    from samurai_ai.domain.kernel import PlanTask

    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "State prompt", OperationMode.BUG_BOUNTY, ScopeContract(includes=())
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import SamuraiApp

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            prompt = app._chat_system_prompt()
            assert "TARGETS: (none)" in prompt
            assert "SCOPE include: (none)" in prompt
            assert "TASKS: (none)" in prompt
            assert "SCOPE is empty" in prompt
            assert "add_scope" in prompt

            app._extend_scope(["example.co.uk"])
            stack[1].create_plan(
                operation.id,
                "Web map",
                [PlanTask(id="map", title="Map the web application")],
                [],
            )
            prompt = app._chat_system_prompt()
            assert "TARGETS: example.co.uk" in prompt
            assert "SCOPE include: example.co.uk" in prompt
            assert "Map the web application" in prompt
            assert "SCOPE is empty" not in prompt
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_a_do_nothing_turn_does_not_report_completed(tmp_path: Path) -> None:
    provider = _NarratingProvider()
    stack = _chat_stack(tmp_path, provider)
    operation = stack[0].create(
        "Repro completion", OperationMode.BUG_BOUNTY, ScopeContract(includes=())
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import SamuraiApp
        from samurai_ai.tui.progress import TerminalReason

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await app._chat("research https://example.co.uk", "gpt/test-model@v1", None)
            assert app._last_turn_reason is TerminalReason.BLOCKED
            assert app.goals.current(operation.id) is not None
            assert app.goals.current(operation.id).block_reason == "finalization_required"
            retries = [
                message
                for message in app._chat_history
                if "Continue the original authorized request" in str(message.content)
            ]
            assert len(retries) == 1
            assert "research https://example.co.uk" in retries[0].content
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_new_message_resumes_blocked_finalization(tmp_path: Path) -> None:
    provider = _NarratingProvider()
    stack = _chat_stack(tmp_path, provider)
    operation = stack[0].create(
        "Repro blocked", OperationMode.BUG_BOUNTY, ScopeContract(includes=())
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import SamuraiApp

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            goal = app.goals.create(operation.id, "research example.co.uk")
            app.goals.block(goal.id, "finalization_required")
            before = provider.calls
            await app._chat("continue please", "gpt/test-model@v1", None)
            # a new operator message resumes the starved goal and buys real work;
            # the repeated refusal may block it again, but cannot be swallowed
            assert provider.calls > before
            assert any(
                event.event_type == "goal.resumed"
                for event in stack[0].events(operation.id)
            )
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_exhausted_goal_still_requires_explicit_budget_extension(tmp_path: Path) -> None:
    provider = _NarratingProvider()
    stack = _chat_stack(tmp_path, provider)
    operation = stack[0].create(
        "Budget boundary", OperationMode.BUG_BOUNTY, ScopeContract(includes=())
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import SamuraiApp

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            goal = app.goals.create(operation.id, "research example.co.uk", budget_steps=1)
            app.goals.record_step(goal.id)
            app.goals.block(goal.id, "finalization_required")
            before = provider.calls
            await app._chat("continue please", "gpt/test-model@v1", None)
            assert provider.calls == before
            assert app.goals.current(operation.id).block_reason == "finalization_required"
            assert not any(
                event.event_type == "goal.resumed"
                for event in stack[0].events(operation.id)
            )
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
