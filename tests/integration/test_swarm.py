"""Integration tests for parallel specialized agent swarms (Stage 5a)."""

import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from samurai_ai.agents.budgets import ModelBudget
from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.agents import AgentRunStatus
from samurai_ai.domain.kernel import PlanTask, RiskClass, TaskStatus
from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelDefinition,
    ProviderDefinition,
)
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.swarm import (
    AgentSpecialization,
    SwarmError,
    SwarmStatus,
    SwarmTaskError,
)
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.swarm import SwarmService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.models import ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.swarm import SwarmRunRepository
from samurai_ai.storage.vault import VaultRepository

TEST_KEY = "ab" * 32

PLANNER_ANSWER = '{"steps": ["do the step"]}'
EXECUTOR_ANSWER = (
    '{"commands": ['
    '{"type": "request_tool_run", "tool_id": "local.echo", "params": {"text": "swarm-probe"}},'
    '{"type": "note", "key": "probe", "value": "done"},'
    '{"type": "complete_task"}'
    "]}"
)
FAIL_ANSWER = '{"commands": [{"type": "fail", "reason": "cannot proceed"}]}'


CLOSE_OUT_ANSWER = '{"commands": [{"type": "complete_task"}]}'


class _SwarmHandler(BaseHTTPRequestHandler):
    queue: list[str] = []
    executor_queue: list[str] = []
    lock = threading.Lock()
    calls = 0
    latency = 0.0
    barrier: threading.Barrier | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with _SwarmHandler.lock:
            _SwarmHandler.calls += 1
            # Executor prompts carry "Current step:" — route them to the
            # executor queue so worker timing cannot mix the roles.
            if b"Current step:" in raw and _SwarmHandler.executor_queue:
                text = _SwarmHandler.executor_queue.pop(0)
            elif b"Current step:" in raw and _SwarmHandler.queue:
                text = _SwarmHandler.queue.pop(0)
            elif b"Current step:" in raw:
                # the queue can run dry on the closing step, which must still close
                text = CLOSE_OUT_ANSWER
            elif _SwarmHandler.queue:
                text = _SwarmHandler.queue.pop(0)
            else:
                text = PLANNER_ANSWER
        if _SwarmHandler.barrier is not None:
            try:
                _SwarmHandler.barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
        if _SwarmHandler.latency:
            time.sleep(_SwarmHandler.latency)
        body = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 8},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def fake_server() -> Iterator[str]:
    _SwarmHandler.queue = []
    _SwarmHandler.executor_queue = []
    _SwarmHandler.calls = 0
    _SwarmHandler.latency = 0.0
    _SwarmHandler.barrier = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SwarmHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def queue_answers(*answers: str) -> None:
    _SwarmHandler.queue = list(answers)


def queue_executor_answers(*answers: str) -> None:
    _SwarmHandler.executor_queue = list(answers)


def make_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    tasks: list[PlanTask],
) -> tuple[OperationService, KernelService, SwarmService, ModelService, str]:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    database = Database(tmp_path / "samurai.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    recovery = RecoveryService(operations, kernel, recovery_repository)
    vault = VaultService(
        VaultRepository(database), operations.repository, tmp_path / "vault.key"
    )
    execution = ToolExecutionService(
        operations, kernel, recovery, ToolRunRepository(database)
    )
    endpoints = ModelEndpointRepository(database)
    registry = ModelRegistry(
        endpoints,
        providers=(
            ProviderDefinition(
                id="fake",
                api_family=ApiFamily.OPENAI_COMPATIBLE,
                base_url=base_url,
                secret_name="fake-key",
            ),
        ),
        models=(
            ModelDefinition(
                id="fake/agent@v1",
                provider_id="fake",
                context_limit=128000,
                capabilities=frozenset(
                    {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOLS}
                ),
            ),
        ),
    )
    models = ModelService(registry, vault, kernel, operations, endpoints, recovery_repository)
    vault.add("fake-key", "unused-key")
    runs = AgentRunRepository(database)
    orchestrator = AgentOrchestrator(models, kernel, execution, operations, runs)
    swarms = SwarmService(operations, kernel, orchestrator, SwarmRunRepository(database))
    operation = operations.create(
        "Swarm lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("example.com",))
    )
    kernel.create_plan(operation.id, "Swarm plan", tasks, [])
    return operations, kernel, swarms, models, operation.id


def approve_model_calls(kernel: KernelService, operation_id: str) -> str:
    approval = kernel.request_approval(
        operation_id, "model.call fake/agent@v1", RiskClass.R2
    )
    kernel.approve(approval.id, decided_by="operator")
    return approval.id


def two_tasks() -> list[PlanTask]:
    return [
        PlanTask(id="recon", title="Recon", risk_class=RiskClass.R1),
        PlanTask(id="testing", title="Testing", risk_class=RiskClass.R1),
    ]


def test_ready_tasks_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    tasks = [
        PlanTask(id="recon", title="Recon"),
        PlanTask(id="testing", title="Testing"),
        PlanTask(id="report", title="Report", depends_on=("recon", "testing")),
    ]
    _, kernel, swarms, _, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, tasks
    )
    assert [task.id for task in swarms.ready_tasks(operation_id)] == ["recon", "testing"]
    kernel.start_task(operation_id, "recon")
    assert [task.id for task in swarms.ready_tasks(operation_id)] == ["testing"]
    kernel.complete_task(operation_id, "recon")
    assert [task.id for task in swarms.ready_tasks(operation_id)] == ["testing"]


def test_swarm_auto_select_completes_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    operations, kernel, swarms, _, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, two_tasks()
    )
    approval_id = approve_model_calls(kernel, operation_id)
    queue_answers(PLANNER_ANSWER, PLANNER_ANSWER)
    queue_executor_answers(EXECUTOR_ANSWER, EXECUTOR_ANSWER)

    swarm = swarms.start(
        operation_id,
        None,
        ["fake/agent@v1"],
        workers=2,
        specializations={"recon": AgentSpecialization.RECON},
        approval_id=approval_id,
    )

    assert swarm.status is SwarmStatus.COMPLETED
    assert set(swarm.task_ids) == {"recon", "testing"}
    plan = kernel.get_current_plan(operation_id)
    assert plan is not None
    assert all(task.status is TaskStatus.DONE for task in plan.tasks)

    members = swarms.members(swarm.id)
    assert len(members) == 2
    by_task = {member.task_id: member for member in members}
    assert by_task["recon"].specialization is AgentSpecialization.RECON
    assert by_task["testing"].specialization is AgentSpecialization.GENERAL
    assert all(member.swarm_id == swarm.id for member in members)
    assert all(member.status is AgentRunStatus.COMPLETED for member in members)

    events = operations.events(operation_id)
    start_events = [e for e in events if e.event_type == "agent.swarm.start"]
    finish_events = [e for e in events if e.event_type == "agent.swarm.finish"]
    assert len(start_events) == 1
    assert start_events[0].payload["specializations"] == {"recon": "recon"}
    assert finish_events[-1].payload["status"] == "completed"
    # run-scoped notes stay namespaced per member
    note_runs = {
        e.payload["run_id"]
        for e in events
        if e.event_type == "agent.note" and e.payload["key"].startswith("run.")
    }
    assert note_runs == {member.id for member in members}


def test_swarm_validation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    _, kernel, swarms, _, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, two_tasks()
    )
    approval_id = approve_model_calls(kernel, operation_id)
    with pytest.raises(SwarmTaskError):
        swarms.start(operation_id, ["missing"], ["fake/agent@v1"], approval_id=approval_id)
    with pytest.raises(SwarmTaskError):
        swarms.start(
            operation_id, ["recon", "recon"], ["fake/agent@v1"], approval_id=approval_id
        )
    with pytest.raises(SwarmTaskError):
        swarms.start(
            operation_id,
            ["recon"],
            ["fake/agent@v1"],
            specializations={"testing": AgentSpecialization.TESTING},
            approval_id=approval_id,
        )
    kernel.start_task(operation_id, "recon")
    with pytest.raises(SwarmTaskError):
        swarms.start(operation_id, ["recon"], ["fake/agent@v1"], approval_id=approval_id)
    with pytest.raises(SwarmError):
        swarms.start(operation_id, None, [], approval_id=approval_id)
    with pytest.raises(SwarmError):
        swarms.start(
            operation_id, None, ["fake/agent@v1"], workers=0, approval_id=approval_id
        )
    with pytest.raises(SwarmError):
        swarms.start(
            operation_id, None, ["fake/agent@v1"], workers=5, approval_id=approval_id
        )


def test_plan_conflict_retry_allows_parallel_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    database = Database(tmp_path / "samurai.db")
    operations = OperationService(OperationRepository(database))
    kernel_repository = KernelRepository(database)
    kernel = KernelService(operations, kernel_repository)
    operation = operations.create(
        "Race lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("example.com",))
    )
    kernel.create_plan(operation.id, "Race plan", two_tasks(), [])
    kernel.start_task(operation.id, "recon")
    kernel.start_task(operation.id, "testing")

    barrier = threading.Barrier(2)
    barrier_lock = threading.Lock()
    reads = 0
    original_get_current_plan = kernel_repository.get_current_plan

    def synchronized_get(operation_id_arg: str) -> object:
        nonlocal reads
        with barrier_lock:
            reads += 1
            first_round = reads <= 2
        plan = original_get_current_plan(operation_id_arg)
        if first_round:
            barrier.wait(timeout=5)
        return plan

    monkeypatch.setattr(kernel_repository, "get_current_plan", synchronized_get)

    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(kernel.complete_task, operation.id, "recon"),
            pool.submit(kernel.complete_task, operation.id, "testing"),
        ]
        for future in futures:
            try:
                future.result(timeout=15)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    assert errors == []
    plan = kernel.get_current_plan(operation.id)
    assert plan is not None
    assert all(task.status is TaskStatus.DONE for task in plan.tasks)


def test_budget_bounded_overshoot_under_parallelism(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    operations, kernel, swarms, models, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, two_tasks()
    )
    approval_id = approve_model_calls(kernel, operation_id)
    models.set_budget(operation_id, ModelBudget(max_calls=1))
    _SwarmHandler.barrier = threading.Barrier(2)
    queue_answers(PLANNER_ANSWER, PLANNER_ANSWER)

    swarm = swarms.start(
        operation_id, None, ["fake/agent@v1"], workers=2, approval_id=approval_id
    )

    usage = models.usage(operation_id)
    # check-then-call is not atomic across threads: overshoot is bounded by the
    # number of in-flight workers, and every call is recorded.
    assert 1 <= usage.calls <= 2
    members = swarms.members(swarm.id)
    assert len(members) == 2
    assert all(
        member.status
        in (AgentRunStatus.BUDGET_EXCEEDED, AgentRunStatus.FAILED)
        for member in members
    )
    assert swarm.status is SwarmStatus.FAILED


def test_swarm_approval_required_marks_members_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    operations, _, swarms, _, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, two_tasks()
    )
    swarm = swarms.start(operation_id, None, ["fake/agent@v1"], workers=2)
    assert swarm.status is SwarmStatus.FAILED
    members = swarms.members(swarm.id)
    assert all(member.status is AgentRunStatus.FAILED for member in members)
    events = operations.events(operation_id)
    failed_events = [e for e in events if e.event_type == "agent.swarm.member-failed"]
    assert len(failed_events) == 2
    assert all("approval required" in e.payload["error"] for e in failed_events)


def test_swarm_partial_when_one_member_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    _, kernel, swarms, _, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, two_tasks()
    )
    approval_id = approve_model_calls(kernel, operation_id)
    # second member's executor answer requests failure
    queue_answers(PLANNER_ANSWER, PLANNER_ANSWER)
    queue_executor_answers(EXECUTOR_ANSWER, FAIL_ANSWER)

    swarm = swarms.start(
        operation_id, None, ["fake/agent@v1"], workers=2, approval_id=approval_id
    )

    assert swarm.status is SwarmStatus.PARTIAL
    statuses = {member.status for member in swarms.members(swarm.id)}
    assert statuses == {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED}


def test_swarm_report_quantifies_wall_time_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    _, kernel, swarms, models, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, two_tasks()
    )
    approval_id = approve_model_calls(kernel, operation_id)
    _SwarmHandler.latency = 0.25
    queue_answers(PLANNER_ANSWER, PLANNER_ANSWER)
    queue_executor_answers(EXECUTOR_ANSWER, EXECUTOR_ANSWER)

    started = time.monotonic()
    swarm = swarms.start(
        operation_id, None, ["fake/agent@v1"], workers=2, approval_id=approval_id
    )
    measured_wall = time.monotonic() - started

    report = swarms.report(swarm.id)
    assert report["status"] == "completed"
    assert len(report["members"]) == 2
    assert report["usage"]["calls"] == 4
    assert report["usage"]["prompt_tokens"] == 160
    assert report["usage"]["completion_tokens"] == 32
    usage = models.usage(operation_id)
    assert usage.calls == report["usage"]["calls"]
    # each member makes 2 calls at 0.25s -> sequential estimate ~1.0s
    assert report["sequential_estimate_seconds"] >= 0.9
    assert report["wall_time_seconds"] is not None
    assert report["wall_time_seconds"] <= measured_wall + 0.01
    assert report["speedup"] is not None and report["speedup"] > 1.3


def test_swarm_persists_across_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    _, kernel, swarms, _, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, two_tasks()
    )
    approval_id = approve_model_calls(kernel, operation_id)
    queue_answers(PLANNER_ANSWER, PLANNER_ANSWER)
    queue_executor_answers(EXECUTOR_ANSWER, EXECUTOR_ANSWER)
    swarm = swarms.start(
        operation_id, None, ["fake/agent@v1"], workers=2, approval_id=approval_id
    )

    database = Database(tmp_path / "samurai.db")
    reopened = SwarmRunRepository(database)
    restored = reopened.get(swarm.id)
    assert restored is not None
    assert restored.status is SwarmStatus.COMPLETED
    members = AgentRunRepository(database).list_swarm_members(swarm.id)
    assert len(members) == 2
    assert all(member.status is AgentRunStatus.COMPLETED for member in members)


def test_a_preset_stop_event_cancels_the_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    """The daemon stops a run by setting an event the worker threads already hold."""
    import threading

    operations, kernel, swarms, _, operation_id = make_stack(
        tmp_path, monkeypatch, fake_server, two_tasks()
    )
    approval_id = approve_model_calls(kernel, operation_id)
    queue_answers(PLANNER_ANSWER, PLANNER_ANSWER)
    queue_executor_answers(EXECUTOR_ANSWER, EXECUTOR_ANSWER)
    stop_event = threading.Event()
    stop_event.set()

    swarm = swarms.start(
        operation_id,
        None,
        ["fake/agent@v1"],
        workers=2,
        approval_id=approval_id,
        stop_event=stop_event,
    )

    assert swarm.status is SwarmStatus.FAILED
    members = swarms.members(swarm.id)
    assert all(member.status is AgentRunStatus.FAILED for member in members)
    events = operations.events(operation_id)
    assert any(
        event.event_type == "agent.run.finish" and "cancelled" in str(event.payload)
        for event in events
    )
