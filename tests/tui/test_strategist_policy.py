"""Auto-strategist is useful only with an attack objective and time to act."""

import asyncio
from pathlib import Path

from test_app import _minimal_stack

from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.tui.app import SamuraiApp


def _exercise(
    tmp_path: Path,
    objective: str,
    spent_steps: int,
    expected_spawns: int,
    budget_seconds: int = 1800,
) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Strategist policy", OperationMode.BUG_BOUNTY, ScopeContract(includes=("target.dev",))
    )

    async def run() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            goal = app.goals.create(
                operation.id,
                objective,
                budget_steps=max(40, spent_steps + 5),
                budget_seconds=budget_seconds,
            )
            for _ in range(spent_steps):
                app.goals.record_step(goal.id)
            spawns: list[str] = []

            async def fake_spawn(instruction: str, **kwargs):  # type: ignore[no-untyped-def]
                spawns.append(instruction)
                return "agent_test"

            app.spawn_subagent = fake_spawn  # type: ignore[method-assign]
            app._strategist_spawned_turn = False
            await app._maybe_spawn_strategist("confirmed documentation fact")
            assert len(spawns) == expected_spawns
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(run())


def test_document_objective_does_not_spawn_attack_strategist(tmp_path: Path) -> None:
    _exercise(
        tmp_path,
        "Go to https://agentreadingtest.com/start/ and follow the instructions",
        0,
        0,
    )


def test_strategist_does_not_consume_the_finalization_reserve(tmp_path: Path) -> None:
    _exercise(tmp_path, "Get a shell and read user.txt", 35, 0)


def test_attack_objective_with_budget_keeps_the_strategist(tmp_path: Path) -> None:
    _exercise(tmp_path, "Get a shell and read user.txt", 0, 1)


def test_strategist_respects_the_seconds_reserve(tmp_path: Path) -> None:
    _exercise(tmp_path, "Get a shell and read user.txt", 0, 0, budget_seconds=60)
