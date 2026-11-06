"""Stage 3 acceptance criteria as executable tests.

1. One playbook runs on two different API families (and, opt-in, on a local
   Ollama model) without changing policy or evidence format.
2. Unsupported capabilities are detected before the run starts.
3. The model cannot change state outside the allowed commands and schemas.
4. Budgets stop the run without losing state; a new run resumes from the
   blackboard.
"""

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.agents.budgets import ModelBudget
from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.agents import AgentRunStatus
from samurai_ai.domain.kernel import PlanTask, RiskClass, StageGate, TaskStatus
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

PLANNER_ANSWER = '{"steps": ["probe the endpoint"]}'
EXECUTOR_ANSWER = (
    '{"commands": ['
    '{"type": "request_tool_run", "tool_id": "local.echo", "params": {"text": "acceptance-probe"}},'
    '{"type": "note", "key": "shared.probe", "value": "probe done"},'
    '{"type": "complete_task", "no_finding": "scripted"}'
    "]}"
)


class _QueuedHandler(BaseHTTPRequestHandler):
    queue: list[str] = []
    family = "openai"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        json.loads(self.rfile.read(length))
        text = self.queue.pop(0) if self.queue else PLANNER_ANSWER
        if self.family == "anthropic":
            self._reply(
                200,
                {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 40, "output_tokens": 8},
                },
            )
        else:
            self._reply(
                200,
                {
                    "choices": [{"message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 8},
                },
            )

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _OpenAIHandler(_QueuedHandler):
    queue: list[str] = []
    family = "openai"


class _AnthropicHandler(_QueuedHandler):
    queue: list[str] = []
    family = "anthropic"


@pytest.fixture
def family_servers() -> Iterator[dict[str, str]]:
    instances: list[HTTPServer] = []
    urls: dict[str, str] = {}
    for name, handler in (("openai", _OpenAIHandler), ("anthropic", _AnthropicHandler)):
        handler.queue = []
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        instances.append(server)
        urls[name] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield urls
    finally:
        for server in instances:
            server.shutdown()
            server.server_close()


def make_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    family: str,
    base_url: str,
) -> tuple[OperationService, KernelService, AgentOrchestrator, str]:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    database = Database(tmp_path / name)
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    recovery = RecoveryService(operations, kernel, recovery_repository)
    vault = VaultService(
        VaultRepository(database), operations.repository, tmp_path / f"{name}.vault.key"
    )
    execution = ToolExecutionService(
        operations, kernel, recovery, ToolRunRepository(database)
    )
    endpoints = ModelEndpointRepository(database)
    api_family = (
        ApiFamily.ANTHROPIC if family == "anthropic" else ApiFamily.OPENAI_COMPATIBLE
    )
    registry = ModelRegistry(
        endpoints,
        providers=(
            ProviderDefinition(
                id="fake",
                api_family=api_family,
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
    models = ModelService(
        registry, vault, kernel, operations, endpoints, recovery_repository
    )
    vault.add("fake-key", "unused-key")
    runs = AgentRunRepository(database)
    orchestrator = AgentOrchestrator(models, kernel, execution, operations, runs)
    operation = operations.create(
        "Acceptance lab", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    )
    kernel.create_plan(
        operation.id,
        "Acceptance plan",
        [
            PlanTask(
                id="probe",
                title="Probe the target",
                risk_class=RiskClass.R1,
            )
        ],
        [StageGate(id="probe-done", name="Probe complete")],
    )
    kernel.start_task(operation.id, "probe")
    return operations, kernel, orchestrator, operation.id


def approve_model_calls(
    kernel: KernelService, operation_id: str, model_id: str = "fake/agent@v1"
) -> str:
    approval = kernel.request_approval(
        operation_id, f"model.call {model_id}", RiskClass.R2
    )
    kernel.approve(approval.id, decided_by="operator")
    return approval.id


def run_playbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    family: str,
    base_url: str,
    queue: list[str],
) -> tuple[OperationService, AgentOrchestrator, str, str]:
    service, kernel, orchestrator, operation_id = make_stack(
        tmp_path, monkeypatch, name, family, base_url
    )
    approval_id = approve_model_calls(kernel, operation_id)
    queue.extend([PLANNER_ANSWER, EXECUTOR_ANSWER])
    run = orchestrator.run_task(
        operation_id, "probe", ["fake/agent@v1"], approval_id=approval_id
    )
    return service, orchestrator, operation_id, run.id


def test_same_playbook_on_two_api_families(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family_servers: dict[str, str]
) -> None:
    results = {}
    for family in ("openai", "anthropic"):
        queue = _OpenAIHandler.queue if family == "openai" else _AnthropicHandler.queue
        results[family] = run_playbook(
            tmp_path,
            monkeypatch,
            f"{family}.db",
            family,
            family_servers[family] + ("/v1" if family == "openai" else "/v1"),
            queue,
        )

    for family, (service, orchestrator, operation_id, run_id) in results.items():
        run = orchestrator.get_run(run_id)
        assert run.status is AgentRunStatus.COMPLETED, family
        plan = service.events(operation_id)
        assert plan, family

    events_a = [
        event.event_type for event in results["openai"][0].events(results["openai"][2])
    ]
    events_b = [
        event.event_type for event in results["anthropic"][0].events(results["anthropic"][2])
    ]
    assert events_a == events_b
    for expected in (
        "agent.run.start",
        "agent.step",
        "agent.note",
        "model.call",
        "tool.run.completed",
        "agent.run.finish",
    ):
        assert expected in events_a

    for _family, (service, _orch, operation_id, run_id) in results.items():
        calls = [
            event
            for event in service.events(operation_id)
            if event.event_type == "model.call"
        ]
        assert all(event.payload["agent_run_id"] == run_id for event in calls)
        assert all(event.payload["local"] is False for event in calls)


def test_artifact_format_identical_across_families(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family_servers: dict[str, str]
) -> None:
    import re

    digests = {}
    for family in ("openai", "anthropic"):
        service, kernel, orchestrator, operation_id = make_stack(
            tmp_path,
            monkeypatch,
            f"artifacts-{family}.db",
            family,
            family_servers[family] + "/v1",
        )
        approval_id = approve_model_calls(kernel, operation_id)
        queue = _OpenAIHandler.queue if family == "openai" else _AnthropicHandler.queue
        queue.extend([PLANNER_ANSWER, EXECUTOR_ANSWER])
        run = orchestrator.run_task(
            operation_id, "probe", ["fake/agent@v1"], approval_id=approval_id
        )
        assert run.status is AgentRunStatus.COMPLETED
        artifacts = kernel.kernel.list_artifacts(operation_id)
        assert artifacts, family
        for artifact in artifacts:
            assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256)
            digests.setdefault(artifact.summary, set()).add(artifact.sha256)
        approval_events = [
            event
            for event in service.events(operation_id)
            if event.event_type.startswith("approval.")
        ]
        assert approval_events, family
    for summary, digest_set in digests.items():
        assert len(digest_set) == 1, summary


def test_unsupported_capability_blocks_before_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, orchestrator, operation_id = make_stack(
        tmp_path, monkeypatch, "caps.db", "openai", "http://127.0.0.1:1/v1"
    )
    orchestrator.models.registry.static_models = (
        ModelDefinition(
            id="fake/limited@v1",
            provider_id="fake",
            capabilities=frozenset({ModelCapability.STREAMING}),
        ),
    )
    with pytest.raises(CapabilityError, match="structured-output"):
        orchestrator.run_task(operation_id, "probe", ["fake/limited@v1"])
    events = service.events(operation_id)
    assert not [event for event in events if event.event_type == "model.call"]
    assert orchestrator.list_runs(operation_id) == []


def test_model_cannot_change_state_outside_allowed_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family_servers: dict[str, str]
) -> None:
    service, kernel, orchestrator, operation_id = make_stack(
        tmp_path, monkeypatch, "hostile.db", "openai", family_servers["openai"] + "/v1"
    )
    before_plan = kernel.get_current_plan(operation_id)
    assert before_plan is not None
    before_plan_json = before_plan.model_dump_json()
    before_artifacts = len(kernel.kernel.list_artifacts(operation_id))

    approval_id = approve_model_calls(kernel, operation_id)
    _OpenAIHandler.queue.extend(
        [
            PLANNER_ANSWER,
            '{"commands": ['
            '{"type": "request_tool_run", "tool_id": "missing.tool", "params": {}},'
            '{"type": "request_tool_run", "tool_id": "local.echo", "params": {}},'
            '{"type": "request_tool_run", "tool_id": "process.exec",'
            ' "params": {"argv": ["cat", "../../etc/passwd"]}},'
            '{"type": "request_tool_run", "tool_id": "http.fetch",'
            ' "params": {"url": "http://10.9.9.9/exfil"}}'
            "]}",
        ]
    )
    run = orchestrator.run_task(
        operation_id, "probe", ["fake/agent@v1"], approval_id=approval_id
    )
    assert run.status is AgentRunStatus.FAILED

    events = service.events(operation_id)
    rejected = [event for event in events if event.event_type == "agent.command.rejected"]
    assert len(rejected) == 4
    assert not [
        event for event in events if event.event_type == "tool.run.completed"
    ]
    after_plan = kernel.get_current_plan(operation_id)
    assert after_plan is not None
    assert after_plan.model_dump_json() == before_plan_json
    assert len(kernel.kernel.list_artifacts(operation_id)) == before_artifacts


def test_schema_violating_output_is_parser_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family_servers: dict[str, str]
) -> None:
    service, kernel, orchestrator, operation_id = make_stack(
        tmp_path, monkeypatch, "schema.db", "openai", family_servers["openai"] + "/v1"
    )
    approval_id = approve_model_calls(kernel, operation_id)
    _OpenAIHandler.queue.extend(
        [
            PLANNER_ANSWER,
            '{"commands": [{"type": "delete_operation", "id": "op_all"}]}',
            '{"commands": [{"type": "delete_operation", "id": "op_all"}]}',
            '{"commands": [{"type": "delete_operation", "id": "op_all"}]}',
        ]
    )
    run = orchestrator.run_task(
        operation_id, "probe", ["fake/agent@v1"], approval_id=approval_id
    )
    assert run.status is AgentRunStatus.FAILED
    events = service.events(operation_id)
    assert not [event for event in events if event.event_type.startswith("tool.run")]
    finish = [event for event in events if event.event_type == "agent.run.finish"]
    assert "schema validation" in finish[-1].payload["reason"]


def test_budget_stop_and_resume_without_state_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family_servers: dict[str, str]
) -> None:
    service, kernel, orchestrator, operation_id = make_stack(
        tmp_path, monkeypatch, "budget.db", "openai", family_servers["openai"] + "/v1"
    )
    approval_id = approve_model_calls(kernel, operation_id)
    orchestrator.models.set_budget(operation_id, ModelBudget(max_calls=2))
    _OpenAIHandler.queue.extend(
        [
            '{"steps": ["first", "second"]}',
            '{"commands": [{"type": "note", "key": "shared.step1", "value": "done"}]}',
        ]
    )
    run = orchestrator.run_task(
        operation_id, "probe", ["fake/agent@v1"], approval_id=approval_id
    )
    assert run.status is AgentRunStatus.BUDGET_EXCEEDED

    events = service.events(operation_id)
    notes = [event for event in events if event.event_type == "agent.note"]
    assert notes and notes[0].payload["key"] == "shared.step1"
    usage = orchestrator.models.usage(operation_id)
    assert usage.calls == 2
    plan = kernel.get_current_plan(operation_id)
    assert plan is not None
    assert plan.task_by_id("probe").status == TaskStatus.IN_PROGRESS  # type: ignore[union-attr]

    orchestrator.models.set_budget(operation_id, ModelBudget(max_calls=10))
    _OpenAIHandler.queue.extend(
        [
            PLANNER_ANSWER,
            EXECUTOR_ANSWER,
        ]
    )
    resumed = orchestrator.run_task(
        operation_id, "probe", ["fake/agent@v1"], approval_id=approval_id
    )
    assert resumed.status is AgentRunStatus.COMPLETED
    assert resumed.id != run.id
    usage = orchestrator.models.usage(operation_id)
    assert usage.calls == 4


@pytest.mark.skipif(
    os.environ.get("SAMURAI_TEST_OLLAMA") != "1",
    reason="opt-in ollama acceptance test (set SAMURAI_TEST_OLLAMA=1)",
)
def test_playbook_on_local_ollama(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from samurai_ai.agents.ollama import OllamaProvider

    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    provider_def = ProviderDefinition(
        id="ollama", api_family=ApiFamily.OLLAMA, base_url="http://localhost:11434",
        local=True,
    )
    provider = OllamaProvider(provider_def)
    if not asyncio.run(provider.health()):
        pytest.skip("ollama daemon is not available")
    discovered = asyncio.run(provider.list_models())
    if not discovered:
        pytest.skip("ollama has no models pulled")
    model = next(
        (entry for entry in discovered if ModelCapability.TOOLS in entry.capabilities),
        discovered[0],
    )

    database = Database(tmp_path / "ollama-acc.db")
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
    registry = ModelRegistry(endpoints, providers=(provider_def,))
    models = ModelService(
        registry, vault, kernel, operations, endpoints, recovery_repository
    )
    orchestrator = AgentOrchestrator(
        models, kernel, execution, operations, AgentRunRepository(database)
    )
    operation = operations.create(
        "Ollama acceptance", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    )
    kernel.create_plan(
        operation.id,
        "Ollama plan",
        [PlanTask(id="probe", title="Say hello and finish", risk_class=RiskClass.R1)],
        [StageGate(id="done", name="Done")],
    )
    kernel.start_task(operation.id, "probe")

    run = orchestrator.run_task(operation.id, "probe", [model.id], max_steps=2)
    assert run.status in (AgentRunStatus.COMPLETED, AgentRunStatus.FAILED)
    events = operations.events(operation.id)
    assert [event for event in events if event.event_type == "agent.run.start"]
    assert [event for event in events if event.event_type == "model.call"]
    if run.status is AgentRunStatus.FAILED:
        finish = [event for event in events if event.event_type == "agent.run.finish"]
        assert finish and finish[-1].payload["status"] == "failed"
