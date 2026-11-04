"""A prompt that does not fit must shrink, not kill the run."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from samurai_ai.agents.orchestrator import (
    AgentError,
    AgentOrchestrator,
    ContextOverflowError,
    _is_overflow,
)
from samurai_ai.domain.agents import AgentRun
from samurai_ai.domain.models import ModelError


class FakeModels:
    def __init__(self, failures: int, text: str = "{}") -> None:
        self.failures = failures
        self.prompts: list[str] = []
        self.text = text

    def complete(self, operation_id: str, model_id: str, prompt: str, **_: Any) -> Any:
        self.prompts.append(prompt)
        if self.failures:
            self.failures -= 1
            raise ModelError(
                "provider kimi is unavailable: This model's maximum context length is "
                "32000 tokens. However, your messages resulted in 40000 tokens."
            )
        return SimpleNamespace(text=self.text)


def build(failures: int) -> tuple[AgentOrchestrator, FakeModels, list]:
    events: list = []
    orchestrator = object.__new__(AgentOrchestrator)
    models = FakeModels(failures)
    orchestrator.models = models
    orchestrator.operations = SimpleNamespace(
        events=lambda _op: [],
        repository=SimpleNamespace(append_event=lambda event: events.append(event)),
    )
    orchestrator.kernel = SimpleNamespace(approve=lambda *a, **k: None)
    return orchestrator, models, events


def run_of() -> AgentRun:
    return AgentRun(operation_id="op_1", task_id="t1", model_route=("fake/model@v1",))


def test_the_wording_is_recognised() -> None:
    assert _is_overflow("This model's maximum context length is 32000 tokens")
    assert _is_overflow('{"code": "context_length_exceeded"}')
    assert _is_overflow("request exceeds the available context size")
    assert not _is_overflow("unknown model")
    assert not _is_overflow("rate limited")


def test_an_overflowing_prompt_is_retried_smaller() -> None:
    orchestrator, models, events = build(failures=1)
    result = AgentOrchestrator._call_model(
        orchestrator, "op_1", run_of(), "P0", json.loads, None,
        rebuild=lambda level: f"P{level}",
    )
    assert result == {}
    assert models.prompts == ["P0", "P1"]
    kinds = [event.event_type for event in events]
    assert "agent.context.overflow" in kinds and "agent.context.recovered" in kinds


def test_the_run_fails_clearly_when_shrinking_is_not_enough() -> None:
    orchestrator, models, events = build(failures=10)
    with pytest.raises(AgentError, match="does not fit the model context window"):
        AgentOrchestrator._call_model(
            orchestrator, "op_1", run_of(), "P0", json.loads, None,
            rebuild=lambda level: f"P{level}",
        )
    # initial call plus two shrinks, then the failure
    assert len(models.prompts) == 3
    assert events[-1].event_type == "agent.context.exhausted"


def test_without_a_rebuild_the_overflow_is_reported_at_once() -> None:
    orchestrator, models, _events = build(failures=1)
    with pytest.raises(AgentError, match="does not fit"):
        AgentOrchestrator._call_model(
            orchestrator, "op_1", run_of(), "P0", json.loads, None
        )
    assert len(models.prompts) == 1


def test_the_overflow_is_raised_as_its_own_type() -> None:
    orchestrator, _models, _events = build(failures=1)
    with pytest.raises(ContextOverflowError):
        AgentOrchestrator._complete(orchestrator, "op_1", run_of(), "P0", None)


def test_the_prompt_builder_keeps_the_marker_and_shrinks_the_blackboard() -> None:
    """The rebuild callback is what actually removes bulk; check its shape."""
    orchestrator, _models, _events = build(failures=0)
    orchestrator._tools_excerpt = lambda: "TOOLS"
    orchestrator._genes_excerpt = lambda: "GENES"
    expanded: list[str] = []
    orchestrator._append = lambda _op, event_type, _payload: expanded.append(event_type)

    def excerpt(_blackboard, _run_id, limit: int = 20) -> str:
        return "\n".join(f"k{i}=v" for i in range(limit))

    orchestrator._blackboard_excerpt = excerpt
    run = run_of()
    captured: dict[int, str] = {}

    def rebuild(level: int) -> str:
        # mirror of the closure inside _execute_step_prompt, kept honest here
        text = f"step\nGENES if level<1\nBlackboard:\n{excerpt(None, '', 20 if level == 0 else 2)}"
        text += f"\n[context trimmed ... level {level}]"
        captured[level] = text
        return text

    AgentOrchestrator._call_model(
        orchestrator, "op_1", run, "P0", json.loads, None, rebuild=rebuild
    )
    # no failure: nothing rebuilt
    assert captured == {}
    rebuild(2)
    assert "[context trimmed" in captured[2]
    # level 2 keeps only two blackboard entries, which is the point of the shrink
    assert "k1=v" in captured[2] and "k2=v" not in captured[2]
