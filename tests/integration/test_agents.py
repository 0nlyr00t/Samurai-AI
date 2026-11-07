from pathlib import Path

import pytest

from samurai_ai.agents.budgets import ModelBudget
from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.provider import ChatRequest, ChatResponse, Usage
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.agents import (
    AgentDepthError,
    AgentRunStatus,
)
from samurai_ai.domain.kernel import (
    ArtifactKind,
    FindingStatus,
    PlanTask,
    RiskClass,
    StageGate,
    TaskStatus,
)
from samurai_ai.domain.models import (
    ApiFamily,
    CapabilityError,
    ModelCapability,
    ModelDefinition,
    ProviderDefinition,
)
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.models import ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.vault import VaultRepository

TEST_KEY = "ab" * 32
MODEL_ID = "fake/agent@v1"
LIMITED_MODEL_ID = "fake/limited@v1"


class FakeProvider:
    def __init__(self) -> None:
        self.responses: list[str] = []
        self.requests: list[ChatRequest] = []

    def queue(self, *texts: str) -> None:
        self.responses.extend(texts)

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["agent"]

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeProvider response queue is empty")
        text = self.responses.pop(0)
        return ChatResponse(
            text=text,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            model_id=request.model_id,
            provider_id="fake",
            latency_ms=1,
        )


def make_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "agents.db"
) -> tuple[OperationService, KernelService, ModelService, AgentOrchestrator, FakeProvider]:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    database = Database(tmp_path / name)
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
                base_url="http://127.0.0.1/v1",
                local=True,
            ),
        ),
        models=(
            ModelDefinition(
                id=MODEL_ID,
                provider_id="fake",
                context_limit=128000,
                capabilities=frozenset(
                    {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOLS}
                ),
                cost_input_per_m=0.001,
                cost_output_per_m=0.002,
            ),
            ModelDefinition(
                id=LIMITED_MODEL_ID,
                provider_id="fake",
                context_limit=128000,
                capabilities=frozenset(),
            ),
        ),
    )
    fake = FakeProvider()
    models = ModelService(
        registry,
        vault,
        kernel,
        operations,
        endpoints,
        recovery_repository,
        provider_factory=lambda provider: fake,
    )
    runs = AgentRunRepository(database)
    orchestrator = AgentOrchestrator(models, kernel, execution, operations, runs)
    return operations, kernel, models, orchestrator, fake


def make_operation(service: OperationService, required: tuple[str, ...] = ()) -> str:
    operation = service.create(
        "Agent lab", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    )
    service_id = operation.id
    return service_id


def make_plan(
    kernel: KernelService,
    operation_id: str,
    required: tuple[str, ...] = (),
    *,
    evidence: bool = True,
) -> None:
    """A ready task. `evidence` pre-seeds an artifact the completion gate wants."""
    kernel.create_plan(
        operation_id,
        "Agent plan",
        [
            PlanTask(
                id="probe",
                title="Probe the service",
                risk_class=RiskClass.R1,
                required_artifacts=required,
            )
        ],
        [StageGate(id="probe-done", name="Probe complete")],
    )
    kernel.start_task(operation_id, "probe")
    if evidence:
        kernel.register_artifact(operation_id, ArtifactKind.NOTE, "seed", task_id="probe")


def test_full_orchestrated_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe the endpoint"]}',
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "local.echo", "params": {"text": "agent-echo"}},'
        '{"type": "note", "key": "shared.result", "value": "probe done"},'
        '{"type": "complete_task", "no_finding": "scripted"}'
        "]}",
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.COMPLETED
    assert run.depth == 0
    assert len(fake.requests) == 2

    plan = kernel.get_current_plan(operation_id)
    assert plan is not None
    assert plan.task_by_id("probe").status == TaskStatus.DONE  # type: ignore[union-attr]

    events = service.events(operation_id)
    types = [event.event_type for event in events]
    assert "agent.run.start" in types
    assert "agent.step" in types
    assert "agent.note" in types
    assert "agent.run.finish" in types
    notes = [event for event in events if event.event_type == "agent.note"]
    by_key = {event.payload["key"]: event.payload["value"] for event in notes}
    assert by_key["shared.result"] == "probe done"
    # the executor sees the tool result through the auto-note feedback loop
    tool_notes = [key for key in by_key if ".tool-result.local.echo" in key]
    assert tool_notes
    assert "agent-echo" in by_key[tool_notes[0]]
    # the note carries the artifact ids so a finding can name real evidence
    assert "artifact_ids: art_" in by_key[tool_notes[0]]

    calls = [event for event in events if event.event_type == "model.call"]
    assert calls and all(event.payload["agent_run_id"] == run.id for event in calls)

    runs = orchestrator.list_runs(operation_id)
    assert [entry.id for entry in runs] == [run.id]

    tool_runs = [event for event in events if event.event_type == "tool.run.completed"]
    assert tool_runs


def test_unknown_tool_command_rejected_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["try tool"]}',
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "missing.tool", "params": {}},'
        '{"type": "complete_task", "no_finding": "scripted"}'
        "]}",
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.COMPLETED
    events = service.events(operation_id)
    rejected = [e for e in events if e.event_type == "agent.command.rejected"]
    assert len(rejected) == 1
    assert rejected[0].payload["command"]["tool_id"] == "missing.tool"
    assert "tool not found" in rejected[0].payload["reason"]
    tool_runs = [event for event in events if event.event_type.startswith("tool.run")]
    assert not tool_runs


def test_malformed_output_retries_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["step one"]}',
        "not json at all",
        "still not json",
        '{"wrong": "schema"}',
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.FAILED
    assert len(fake.requests) == 4
    events = service.events(operation_id)
    finish = [e for e in events if e.event_type == "agent.run.finish"]
    assert finish[-1].payload["status"] == "failed"


def test_malformed_output_recovers_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["step one"]}',
        "garbage",
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.COMPLETED
    assert len(fake.requests) == 3
    assert "Parser error" in fake.requests[-1].messages[0].content


def test_propose_finding_unknown_artifact_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["report"]}',
        '{"commands": ['
        '{"type": "propose_finding", "title": "ghost evidence", '
        '"artifact_ids": ["art_missing"]},'
        '{"type": "complete_task", "no_finding": "scripted"}'
        "]}",
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.COMPLETED
    events = service.events(operation_id)
    rejected = [e for e in events if e.event_type == "agent.command.rejected"]
    assert len(rejected) == 1
    assert "unknown artifact" in rejected[0].payload["reason"]
    findings = kernel.kernel.list_findings(operation_id)
    assert findings == []


def test_propose_finding_with_real_artifact_creates_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    artifact = kernel.register_artifact(
        operation_id, ArtifactKind.NOTE, "proof", summary="proof"
    )
    fake.queue(
        '{"steps": ["report"]}',
        '{"commands": ['
        f'{{"type": "propose_finding", "title": "real finding", "severity": "medium", '
        f'"summary": "s", "artifact_ids": ["{artifact.id}"]}},'
        '{"type": "complete_task", "no_finding": "scripted"}'
        "]}",
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.COMPLETED
    findings = kernel.kernel.list_findings(operation_id)
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.CANDIDATE
    assert findings[0].title == "real finding"
    assert artifact.id in findings[0].evidence_ids


def test_complete_task_without_required_artifact_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id, required=("probe-proof",))
    fake.queue(
        '{"steps": ["finish"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
        # retry in the step, then three closing attempts, all refused for the
        # same missing artifact
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.COMPLETED
    plan = kernel.get_current_plan(operation_id)
    assert plan is not None
    # the closing attempts are spent: the task is blocked, not left hanging
    assert plan.task_by_id("probe").status == TaskStatus.BLOCKED  # type: ignore[union-attr]
    events = service.events(operation_id)
    rejected = [e for e in events if e.event_type == "agent.command.rejected"]
    assert rejected and "missing required artifact" in rejected[0].payload["reason"]
    assert any(e.event_type == "task.blocked" for e in events)


def test_budget_exceeded_mid_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    models.set_budget(operation_id, ModelBudget(max_calls=1))
    fake.queue(
        '{"steps": ["step"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.BUDGET_EXCEEDED
    assert len(fake.requests) == 1
    events = service.events(operation_id)
    finish = [e for e in events if e.event_type == "agent.run.finish"]
    assert finish[-1].payload["status"] == "budget-exceeded"
    stored = orchestrator.get_run(run.id)
    assert stored.status is AgentRunStatus.BUDGET_EXCEEDED


def test_capability_precheck_blocks_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    with pytest.raises(CapabilityError, match="structured-output"):
        orchestrator.run_task(operation_id, "probe", [LIMITED_MODEL_ID])
    assert fake.requests == []


def test_subagent_depth_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["s"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    parent = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert parent.depth == 0

    kernel2 = kernel
    make_plan_for_second_task(kernel2, operation_id)
    fake.queue(
        '{"steps": ["s"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    child = orchestrator.run_task(
        operation_id, "probe2", [MODEL_ID], parent_run_id=parent.id
    )
    assert child.depth == 1

    # depth 2 is the helper level now; depth 3 is still refused
    make_plan_for_third_task(kernel2, operation_id)
    fake.queue(
        '{"steps": ["s"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    grandchild = orchestrator.run_task(
        operation_id, "probe3", [MODEL_ID], parent_run_id=child.id
    )
    assert grandchild.depth == 2
    with pytest.raises(AgentDepthError, match="max_depth"):
        orchestrator.run_task(
            operation_id, "probe3", [MODEL_ID], parent_run_id=grandchild.id
        )


def make_plan_for_third_task(kernel: KernelService, operation_id: str) -> None:
    plan = kernel.get_current_plan(operation_id)
    assert plan is not None
    from samurai_ai.domain.kernel import PlanTask, RiskClass

    expected_version = plan.version
    plan.tasks = tuple(plan.tasks) + (
        PlanTask(id="probe3", title="Third", risk_class=RiskClass.R1),
    )
    plan.touch()
    from samurai_ai.domain.operations import OperationEvent

    kernel.kernel.save_plan(
        plan,
        expected_version,
        OperationEvent(
            operation_id=operation_id,
            event_type="plan.revised",
            payload={"plan_id": plan.id},
        ),
    )
    kernel.start_task(operation_id, "probe3")
    kernel.register_artifact(operation_id, ArtifactKind.NOTE, "seed", task_id="probe3")


def make_plan_for_second_task(kernel: KernelService, operation_id: str) -> None:
    plan = kernel.get_current_plan(operation_id)
    assert plan is not None
    from samurai_ai.domain.kernel import PlanTask, RiskClass

    expected_version = plan.version
    plan.tasks = tuple(plan.tasks) + (
        PlanTask(id="probe2", title="Second", risk_class=RiskClass.R1),
    )
    plan.touch()
    from samurai_ai.domain.operations import OperationEvent

    kernel.kernel.save_plan(
        plan,
        expected_version,
        OperationEvent(
            operation_id=operation_id,
            event_type="plan.revised",
            payload={"plan_id": plan.id},
        ),
    )
    kernel.start_task(operation_id, "probe2")
    kernel.register_artifact(operation_id, ArtifactKind.NOTE, "seed", task_id="probe2")


def test_consecutive_rejects_fail_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["spam"]}',
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "missing.a", "params": {}},'
        '{"type": "request_tool_run", "tool_id": "missing.b", "params": {}},'
        '{"type": "request_tool_run", "tool_id": "missing.c", "params": {}},'
        '{"type": "request_tool_run", "tool_id": "missing.d", "params": {}},'
        '{"type": "request_tool_run", "tool_id": "missing.e", "params": {}},'
        '{"type": "request_tool_run", "tool_id": "missing.f", "params": {}}'
        "]}",
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status is AgentRunStatus.FAILED
    events = service.events(operation_id)
    rejected = [e for e in events if e.event_type == "agent.command.rejected"]
    # the whole command batch of the step runs, then the budget trips
    assert len(rejected) == 6
    assert "too many consecutive rejected" in [
        e for e in events if e.event_type == "agent.run.finish"
    ][-1].payload["reason"]


def test_approval_resolver_decides_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch, "res.db")
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "shell.exec", "params": {"command": "echo hi"}}'
        "]}",
        # the task is still open, so the closing step runs and closes it
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    seen: list[str] = []

    def resolver(approval_id: str) -> bool:
        seen.append(approval_id)
        return True

    result = orchestrator.run_task(operation_id, "probe", [MODEL_ID], approval_resolver=resolver)
    assert result.status is AgentRunStatus.COMPLETED
    assert seen
    approvals = [
        approval
        for approval in kernel.kernel.list_approvals(operation_id)
        if approval.action == "tool.run shell.exec"
    ]
    assert approvals and all(approval.decided_by == "subagent" for approval in approvals)


def test_denied_resolver_rejects_the_command_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, models, orchestrator, fake = make_stack(
        tmp_path, monkeypatch, "deny.db"
    )
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "shell.exec", "params": {"command": "echo hi"}},'
        '{"type": "complete_task", "no_finding": "scripted"}'
        "]}",
    )
    run = orchestrator.run_task(
        operation_id, "probe", [MODEL_ID], approval_resolver=lambda approval_id: False
    )
    assert run.status is AgentRunStatus.COMPLETED
    rejected = [
        event
        for event in service.events(operation_id)
        if event.event_type.endswith("rejected")
    ]
    assert rejected
    assert "denied" in str(rejected[0].payload)


def test_stop_event_fails_the_run_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch, "stop.db")
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["a", "b", "c"]}',
        '{"commands": [{"type": "note", "key": "k1", "value": "v"}]}',
    )
    stop = threading.Event()
    stop.set()
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID], stop_event=stop)
    assert run.status is AgentRunStatus.FAILED
    finishes = [
        event
        for event in service.events(operation_id)
        if event.event_type == "agent.run.finish"
    ]
    assert len(finishes) == 1
    assert "cancelled" in str(finishes[0].payload)
    plan = kernel.get_current_plan(operation_id)
    assert plan is not None
    assert plan.task_by_id("probe").status is TaskStatus.IN_PROGRESS  # type: ignore[union-attr]


def test_read_only_only_mode_rejects_mutating_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch, "ro.db")
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "shell.exec", "params": {"command": "ls"}}'
        "]}",
        # the rejection leaves the task open, so the closing step closes it
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    assert orchestrator._is_read_only("shell.exec") is False
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID], read_only_only=True)
    assert run.status is AgentRunStatus.COMPLETED
    rejected = [
        event
        for event in service.events(operation_id)
        if event.event_type.endswith("rejected")
    ]
    assert rejected
    assert "read-only" in str(rejected[0].payload)


def test_a_refused_step_is_asked_again_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": [{"type": "request_tool_run", "tool_id": "missing.tool", "params": {}}]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )

    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    assert run.status is AgentRunStatus.COMPLETED
    retry_prompt = fake.requests[2].messages[-1].content
    assert "[rejected]" in retry_prompt
    assert "tool not found: missing.tool" in retry_prompt
    assert kernel.get_current_plan(operation_id).task_by_id("probe").status is (  # type: ignore[union-attr]
        TaskStatus.DONE
    )


def test_a_partly_accepted_step_is_not_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "missing.tool", "params": {}},'
        '{"type": "complete_task", "no_finding": "scripted"}'
        "]}",
    )

    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    assert run.status is AgentRunStatus.COMPLETED
    # planner + one executor call, and the close-out never fires
    assert len(fake.requests) == 2


def test_completing_an_already_done_task_is_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "missing.tool", "params": {}},'
        '{"type": "complete_task", "no_finding": "scripted"}'
        "]}",
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )

    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    assert run.status is AgentRunStatus.COMPLETED
    reasons = [
        event.payload["reason"]
        for event in service.events(operation_id)
        if event.event_type == "agent.command.rejected"
    ]
    assert reasons == ["tool not found: missing.tool"]


def test_a_fail_next_to_a_tool_call_does_not_end_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": [{"type": "note", "key": "k", "value": "v"}]}',
        # the closing answer blames a tool but the task itself is finished
        '{"commands": ['
        '{"type": "fail", "reason": "tool not found: completed_task"},'
        '{"type": "request_tool_run", "tool_id": "missing.tool", "params": {}}'
        "]}",
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )

    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    assert run.status is AgentRunStatus.COMPLETED
    assert kernel.get_current_plan(operation_id).task_by_id("probe").status is (  # type: ignore[union-attr]
        TaskStatus.DONE
    )


def test_a_third_identical_call_earns_a_repeat_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    same = (
        '{"commands": ['
        '{"type": "request_tool_run", "tool_id": "local.echo", "params": {"text": "again"}},'
        '{"type": "note", "key": "shared.same", "value": "same"},'
        '{"type": "complete_task", "no_finding": "scripted"}'
        "]}"
    )
    fake.queue(
        '{"steps": ["one", "two", "three", "four"]}', same, same, same,
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    orchestrator.run_task(operation_id, "probe", [MODEL_ID], max_steps=4)

    events = service.events(operation_id)
    repeated = [event for event in events if event.event_type == "agent.command.repeated"]
    assert repeated and repeated[0].payload["times"] == 3
    # the nudge reached the model on the following step
    prompts = [
        message.content for message in fake.requests[-1].messages if message.role == "user"
    ]
    assert any("[repeats]" in prompt for prompt in prompts)


def test_the_wall_deadline_ends_a_slow_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["one", "two"]}',
        '{"commands": [{"type": "request_tool_run", "tool_id": "local.echo", '
        '"params": {"text": "x"}}]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    run = orchestrator.run_task(
        operation_id, "probe", [MODEL_ID], max_steps=2, max_wall_seconds=0.0
    )
    assert run.status.value == "failed"
    finished = [
        event.payload
        for event in service.events(operation_id)
        if event.event_type == "agent.run.finish"
    ]
    assert finished and "deadline" in finished[-1]["reason"]


def test_active_genes_reach_the_executor_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.config import genes_path
    from samurai_ai.services.genes import GeneService, GeneStore

    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    genes = GeneService(GeneStore(genes_path()))
    active = genes.create("planner", "prompt-block", "Hint", "check the robots.txt first")
    genes.record_fitness(active.id, samples=3, median_solve_rate=0.9)
    genes.promote(active.id)
    shadow = genes.create("planner", "prompt-block", "Other", "a shadow hint")
    _ = shadow
    orchestrator.genes = genes

    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    kernel.register_artifact(operation_id, ArtifactKind.NOTE, "probe", task_id="probe")
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    prompts = [
        message.content for message in fake.requests[-1].messages if message.role == "user"
    ]
    joined = "\n".join(prompts)
    assert "check the robots.txt first" in joined
    assert "a shadow hint" not in joined


def test_a_run_gets_a_wall_deadline_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.agents import orchestrator as module

    seen: list[float | None] = []
    original = orchestrator_run = None
    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    original = orchestrator._execute_steps

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs.get("max_wall_seconds"))
        return original(*args, **kwargs)

    orchestrator._execute_steps = spy  # type: ignore[method-assign]
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["one"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert seen == [module.DEFAULT_MAX_WALL_SECONDS]
    _ = orchestrator_run


def test_active_genes_reach_the_planner_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.config import genes_path
    from samurai_ai.services.genes import GeneService, GeneStore

    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    genes = GeneService(GeneStore(genes_path()))
    gene = genes.create("planner", "prompt-block", "Hint", "always start with crtsh")
    genes.record_fitness(gene.id, samples=3, median_solve_rate=0.9)
    genes.promote(gene.id)
    orchestrator.genes = genes

    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    # the planner call is the first one: the hint must be there already
    first_prompt = "\n".join(
        message.content for message in fake.requests[0].messages if message.role == "user"
    )
    assert "always start with crtsh" in first_prompt


def test_the_model_can_escalate_impact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.domain.kernel import ArtifactKind
    from samurai_ai.services.impact import ImpactService

    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    artifact = kernel.register_artifact(
        operation_id, ArtifactKind.COMMAND_OUTPUT, "proof", summary="proof"
    )
    finding = kernel.create_finding(operation_id, "IDOR", summary="read orders")
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.validate_finding(finding.id)
    orchestrator.impact = ImpactService(service, kernel)

    fake.queue(
        '{"steps": ["escalate"]}',
        '{"commands": ['
        f'{{"type": "record_impact", "finding_id": "{finding.id}", '
        '"level": "auth-bypass", "evidence": ["'
        f'{artifact.id}"], "rollback": "delete created user"}},'
        '{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    state = orchestrator.impact.state(finding.id)
    assert state.level is not None and state.level.value == "auth-bypass"
    events = [event.event_type for event in service.repository.events(operation_id)]
    assert "agent.command.impact" in events
    assert "impact.attempt" in events


def test_a_bad_impact_command_is_refused_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.domain.kernel import ArtifactKind
    from samurai_ai.services.impact import ImpactService

    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    artifact = kernel.register_artifact(
        operation_id, ArtifactKind.COMMAND_OUTPUT, "proof", summary="proof"
    )
    finding = kernel.create_finding(operation_id, "IDOR")
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.validate_finding(finding.id)
    orchestrator.impact = ImpactService(service, kernel)

    # write without a rollback note is refused, the run survives
    fake.queue(
        '{"steps": ["escalate"]}',
        '{"commands": ['
        f'{{"type": "record_impact", "finding_id": "{finding.id}", '
        f'"level": "write", "evidence": ["{artifact.id}"]}}]}}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])
    assert run.status.value == "completed"
    rejected = [
        event for event in service.events(operation_id)
        if event.event_type == "agent.command.rejected"
    ]
    assert rejected and "rollback" in str(rejected[0].payload)


def test_the_model_asks_the_operator_and_keeps_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question nobody answers must not stall the run."""
    from samurai_ai.services.questions import Answer, OperatorQuestionService
    from samurai_ai.storage.questions import QuestionRepository

    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    questions = OperatorQuestionService(service, QuestionRepository(kernel.kernel.database))
    orchestrator.questions = questions
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["decide"]}',
        '{"commands": [{"type": "ask_operator", "timeout": 1, "questions": '
        '[{"id": "next", "question": "Go deeper?", "header": "Decide", '
        '"options": [{"label": "Yes", "recommended": true}, {"label": "No"}]}]}]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    assert run.status is AgentRunStatus.COMPLETED, run.notes
    events = [event.event_type for event in service.repository.events(operation_id)]
    assert "agent.question.asked" in events, events
    pending = questions.pending(operation_id)
    assert pending, "the question must stay listed after the deadline"
    assert pending[0].status == "expired"
    rows = questions.answer(
        pending[0].call_id, [Answer(id="next", selected=("Yes",))], "op"
    )
    assert rows[0].status == "answered"
    # answered calls drop out of the open list
    assert pending[0].call_id not in {row.call_id for row in questions.pending(operation_id)}


def test_an_answered_question_reaches_the_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.services.questions import Answer, OperatorQuestionService
    from samurai_ai.storage.questions import QuestionRepository

    service, kernel, models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    questions = OperatorQuestionService(service, QuestionRepository(kernel.kernel.database))
    orchestrator.questions = questions

    def notice(call_id: str) -> None:
        questions.answer(call_id, [Answer(id="next", selected=("Yes",))], "op")

    orchestrator.question_notice = notice
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["decide"]}',
        '{"commands": [{"type": "ask_operator", "questions": '
        '[{"id": "next", "question": "Go deeper?", '
        '"options": [{"label": "Yes"}, {"label": "No"}]}]}]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    assert run.status is AgentRunStatus.COMPLETED
    later_prompt = "\n".join(
        message.content for message in fake.requests[-1].messages if message.role == "user"
    )
    assert '"selected": ["Yes"]' in later_prompt


def test_closing_without_a_finding_leaves_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty report is the quiet failure mode; the note says it out loud."""
    service, kernel, _, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )
    orchestrator.run_task(operation_id, "probe", [MODEL_ID])

    notes = [
        event.payload["key"]
        for event in service.events(operation_id)
        if event.event_type == "agent.note"
    ]
    assert any("closed-without-finding" in key for key in notes)
