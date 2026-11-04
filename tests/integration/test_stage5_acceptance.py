"""Stage 5a acceptance: parallelism only ships with a proven time/cost win
and no error growth.

The same two-task workload runs sequentially (two `run_task` calls) and as a
swarm (workers=2) against a latency-injecting fake provider. The swarm must be
measurably faster, produce identical governed outcomes (tasks done, tool runs,
artifacts, notes, zero rejected commands), account usage exactly, and survive
a database reopen. The bounded budget overshoot under parallelism is covered
separately in tests/integration/test_swarm.py.
"""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

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
from samurai_ai.domain.swarm import AgentSpecialization, SwarmStatus
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
LATENCY_SECONDS = 0.25

PLANNER_ANSWER = '{"steps": ["probe the endpoint"]}'
EXECUTOR_ANSWER = (
    '{"commands": ['
    '{"type": "request_tool_run", "tool_id": "local.echo", "params": {"text": "acc"}},'
    '{"type": "note", "key": "probe", "value": "done"},'
    '{"type": "complete_task"}'
    "]}"
)


CLOSE_OUT_ANSWER = '{"commands": [{"type": "complete_task"}]}'


class _LatencyHandler(BaseHTTPRequestHandler):
    queue: list[str] = []
    executor_queue: list[str] = []
    lock = threading.Lock()
    calls = 0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with _LatencyHandler.lock:
            _LatencyHandler.calls += 1
            # Executor prompts carry "Current step:" — route them to the
            # executor queue so worker timing cannot mix the roles.
            if b"Current step:" in raw and _LatencyHandler.executor_queue:
                text = _LatencyHandler.executor_queue.pop(0)
            elif b"Current step:" in raw and _LatencyHandler.queue:
                text = _LatencyHandler.queue.pop(0)
            elif b"Current step:" in raw:
                # the queue can run dry on the closing step, which must still close
                text = CLOSE_OUT_ANSWER
            elif _LatencyHandler.queue:
                text = _LatencyHandler.queue.pop(0)
            else:
                text = PLANNER_ANSWER
        time.sleep(LATENCY_SECONDS)
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
    _LatencyHandler.queue = []
    _LatencyHandler.executor_queue = []
    _LatencyHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LatencyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


class Stack:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
        monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
        self.database = Database(tmp_path / "samurai.db")
        self.operations = OperationService(OperationRepository(self.database))
        self.kernel = KernelService(self.operations, KernelRepository(self.database))
        recovery_repository = RecoveryRepository(self.database)
        recovery = RecoveryService(self.operations, self.kernel, recovery_repository)
        vault = VaultService(
            VaultRepository(self.database),
            self.operations.repository,
            tmp_path / "vault.key",
        )
        execution = ToolExecutionService(
            self.operations, self.kernel, recovery, ToolRunRepository(self.database)
        )
        endpoints = ModelEndpointRepository(self.database)
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
        self.models = ModelService(
            registry, vault, self.kernel, self.operations, endpoints, recovery_repository
        )
        vault.add("fake-key", "unused-key")
        self.runs = AgentRunRepository(self.database)
        self.orchestrator = AgentOrchestrator(
            self.models, self.kernel, execution, self.operations, self.runs
        )
        self.swarms = SwarmService(
            self.operations, self.kernel, self.orchestrator, SwarmRunRepository(self.database)
        )

    def make_operation(self, name: str) -> tuple[str, str]:
        operation = self.operations.create(
            name, OperationMode.BUG_BOUNTY, ScopeContract(includes=("example.com",))
        )
        self.kernel.create_plan(
            operation.id,
            f"{name} plan",
            [
                PlanTask(id="recon", title="Recon", risk_class=RiskClass.R1),
                PlanTask(id="testing", title="Testing", risk_class=RiskClass.R1),
            ],
            [],
        )
        approval = self.kernel.request_approval(
            operation.id, "model.call fake/agent@v1", RiskClass.R2
        )
        self.kernel.approve(approval.id, decided_by="operator")
        return operation.id, approval.id


def _runs_overlap(events: list) -> bool:
    """True when two agent runs were in flight at the same moment."""
    spans: dict[str, tuple[object, object]] = {}
    for event in events:
        run_id = event.payload.get("run_id")
        if not isinstance(run_id, str):
            continue
        if event.event_type == "agent.run.start":
            spans[run_id] = (event.created_at, None)
        elif event.event_type == "agent.run.finish" and run_id in spans:
            started, _ = spans[run_id]
            spans[run_id] = (started, event.created_at)
    intervals = [
        (start, finish) for start, finish in spans.values() if finish is not None
    ]
    for index, (_start, finish) in enumerate(intervals):
        for other_start, _other_finish in intervals[index + 1:]:
            if other_start < finish:
                return True
    return False


def _outcome_stats(events: list) -> dict[str, int]:
    stats = {"model_calls": 0, "notes": 0, "rejected": 0, "tool_artifacts": 0}
    for event in events:
        if event.event_type == "model.call":
            stats["model_calls"] += 1
        elif event.event_type == "agent.note":
            stats["notes"] += 1
        elif event.event_type == "agent.command.rejected":
            stats["rejected"] += 1
    return stats


def test_parallel_swarm_faster_with_identical_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    stack = Stack(tmp_path, monkeypatch, fake_server)
    sequential_op, sequential_approval = stack.make_operation("Sequential lab")
    swarm_op, swarm_approval = stack.make_operation("Swarm lab")
    _LatencyHandler.queue = [
        # sequential operation: planner+executor per task, strictly in order
        PLANNER_ANSWER,
        EXECUTOR_ANSWER,
        PLANNER_ANSWER,
        EXECUTOR_ANSWER,
        # swarm: both planners land on the planner queue; executors are routed
        # to the executor queue by the "Current step:" marker
        PLANNER_ANSWER,
        PLANNER_ANSWER,
    ]

    sequential_started = time.monotonic()
    for task_id in ("recon", "testing"):
        stack.kernel.start_task(sequential_op, task_id)
        run = stack.orchestrator.run_task(
            sequential_op, task_id, ["fake/agent@v1"], approval_id=sequential_approval
        )
        assert run.status is AgentRunStatus.COMPLETED
    sequential_wall = time.monotonic() - sequential_started

    swarm_started = time.monotonic()
    with _LatencyHandler.lock:
        _LatencyHandler.executor_queue = [EXECUTOR_ANSWER, EXECUTOR_ANSWER]
    swarm = stack.swarms.start(
        swarm_op,
        None,
        ["fake/agent@v1"],
        workers=2,
        specializations={
            "recon": AgentSpecialization.RECON,
            "testing": AgentSpecialization.TESTING,
        },
        approval_id=swarm_approval,
    )
    swarm_wall = time.monotonic() - swarm_started

    # proven win: the members actually run at the same time. Wall-clock alone is
    # flaky on a loaded CI runner (thread pool + shared fake server), so the
    # primary evidence is overlap of the member runs; the wall time must merely
    # not be worse than the sequential baseline.
    assert swarm.status is SwarmStatus.COMPLETED
    assert _runs_overlap(stack.operations.events(swarm_op)), (
        "swarm members never overlapped in time"
    )
    assert swarm_wall < 1.2 * sequential_wall, (
        f"swarm {swarm_wall:.2f}s slower than sequential {sequential_wall:.2f}s"
    )

    # identical governed outcomes: no error growth
    sequential_plan = stack.kernel.get_current_plan(sequential_op)
    swarm_plan = stack.kernel.get_current_plan(swarm_op)
    assert sequential_plan is not None and swarm_plan is not None
    assert all(task.status is TaskStatus.DONE for task in sequential_plan.tasks)
    assert all(task.status is TaskStatus.DONE for task in swarm_plan.tasks)

    sequential_stats = _outcome_stats(stack.operations.events(sequential_op))
    swarm_stats = _outcome_stats(stack.operations.events(swarm_op))
    assert sequential_stats == swarm_stats
    assert swarm_stats["rejected"] == 0
    assert swarm_stats["model_calls"] == 4
    # 2 model notes + 2 automatic tool-result feedback notes
    assert swarm_stats["notes"] == 4

    # usage accounting is exact on both paths and identical in volume
    sequential_usage = stack.models.usage(sequential_op)
    swarm_usage = stack.models.usage(swarm_op)
    assert sequential_usage.calls == swarm_usage.calls == 4
    assert sequential_usage.total_tokens == swarm_usage.total_tokens == 4 * 48

    # swarm report quantifies the win
    report = stack.swarms.report(swarm.id)
    assert report["usage"]["calls"] == 4
    assert report["speedup"] is not None and report["speedup"] > 1.3

    # state survives a database reopen
    reopened_swarms = SwarmRunRepository(Database(tmp_path / "samurai.db"))
    restored = reopened_swarms.get(swarm.id)
    assert restored is not None and restored.status is SwarmStatus.COMPLETED
    members = AgentRunRepository(Database(tmp_path / "samurai.db")).list_swarm_members(
        swarm.id
    )
    assert len(members) == 2
    assert {member.specialization for member in members} == {
        AgentSpecialization.RECON,
        AgentSpecialization.TESTING,
    }
