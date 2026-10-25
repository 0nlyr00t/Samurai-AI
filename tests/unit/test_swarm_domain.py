import pytest
from pydantic import ValidationError

from samurai_ai.domain.agents import AgentRun
from samurai_ai.domain.swarm import (
    MAX_SWARM_WORKERS,
    AgentSpecialization,
    SwarmError,
    SwarmRun,
    SwarmStatus,
    SwarmTaskError,
)


def test_swarm_run_defaults() -> None:
    swarm = SwarmRun(
        operation_id="op1",
        workers=2,
        task_ids=("recon", "testing"),
        model_route=("fake/agent@v1",),
    )
    assert swarm.id.startswith("swarm_")
    assert swarm.status is SwarmStatus.RUNNING
    assert swarm.created_by == "operator"
    assert swarm.finished_at is None


def test_swarm_finish_transitions() -> None:
    swarm = SwarmRun(
        operation_id="op1", workers=1, task_ids=("t1",), model_route=("m@v1",)
    )
    swarm.finish(SwarmStatus.COMPLETED)
    assert swarm.status is SwarmStatus.COMPLETED
    assert swarm.finished_at is not None
    with pytest.raises(SwarmError):
        swarm.finish(SwarmStatus.FAILED)


def test_swarm_cannot_finish_as_running() -> None:
    swarm = SwarmRun(
        operation_id="op1", workers=1, task_ids=("t1",), model_route=("m@v1",)
    )
    with pytest.raises(SwarmError):
        swarm.finish(SwarmStatus.RUNNING)


def test_swarm_workers_bounds() -> None:
    with pytest.raises(ValidationError):
        SwarmRun(operation_id="op1", workers=0, task_ids=("t1",), model_route=("m@v1",))
    with pytest.raises(ValidationError):
        SwarmRun(
            operation_id="op1",
            workers=MAX_SWARM_WORKERS + 1,
            task_ids=("t1",),
            model_route=("m@v1",),
        )
    swarm = SwarmRun(
        operation_id="op1",
        workers=MAX_SWARM_WORKERS,
        task_ids=("t1",),
        model_route=("m@v1",),
    )
    assert swarm.workers == MAX_SWARM_WORKERS


def test_swarm_requires_tasks_and_route() -> None:
    with pytest.raises(ValidationError):
        SwarmRun(operation_id="op1", workers=1, task_ids=(), model_route=("m@v1",))
    with pytest.raises(ValidationError):
        SwarmRun(operation_id="op1", workers=1, task_ids=("t1",), model_route=())


def test_specialization_values() -> None:
    assert AgentSpecialization.GENERAL.value == "general"
    assert {item.value for item in AgentSpecialization} == {
        "general",
        "recon",
        "testing",
        "analysis",
    }


def test_swarm_task_error_is_swarm_error() -> None:
    assert issubclass(SwarmTaskError, SwarmError)
    assert issubclass(SwarmError, ValueError)


def test_agent_run_swarm_fields_default() -> None:
    run = AgentRun(operation_id="op1", task_id="t1")
    assert run.swarm_id is None
    assert run.specialization is AgentSpecialization.GENERAL


def test_agent_run_swarm_fields_assignable() -> None:
    run = AgentRun(
        operation_id="op1",
        task_id="t1",
        swarm_id="swarm_abc",
        specialization=AgentSpecialization.RECON,
    )
    assert run.swarm_id == "swarm_abc"
    assert run.specialization is AgentSpecialization.RECON
