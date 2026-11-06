"""Stage 5b acceptance: campaign memory (FR-17) and custom playbooks/skills (FR-18).

E2E over a prompt-capturing fake provider:
- memory curated in a workspace is injected read-only into agent runs of
  *different* operations, with vault secrets redacted and an audit trail in
  ``agent.run.start``; retention purge and deletion work (memory is not
  evidence);
- a custom playbook installs after structural validation, creates a governed
  plan via the kernel, and a swarm completes it; trusted skills land in
  executor prompts; untrusted installation is refused.
"""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.agents import AgentRunStatus
from samurai_ai.domain.kernel import ArtifactKind, PlanTask, RiskClass, TaskStatus
from samurai_ai.domain.memory import MemoryKind, MemoryNotFoundError
from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelDefinition,
    ProviderDefinition,
)
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.playbooks import PlaybookError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.skills import SkillTrustError
from samurai_ai.domain.swarm import SwarmStatus
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.skills import SkillService
from samurai_ai.services.swarm import SwarmService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.memory import MemoryRepository
from samurai_ai.storage.models import ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.playbooks import PlaybookRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.skills import SkillRepository
from samurai_ai.storage.swarm import SwarmRunRepository
from samurai_ai.storage.vault import VaultRepository

TEST_KEY = "ab" * 32

PLANNER_ANSWER = '{"steps": ["do it"]}'
EXECUTOR_ANSWER = (
    '{"commands": ['
    '{"type": "note", "key": "done", "value": "yes"},'
    '{"type": "complete_task", "no_finding": "scripted"}'
    "]}"
)


CLOSE_OUT_ANSWER = '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}'


_PLANNER_MARKER = "You are the planner"


class _CapturingHandler(BaseHTTPRequestHandler):
    queue: list[str] = []
    executor_queue: list[str] = []
    prompts: list[str] = []
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        payload = json.loads(raw)
        with _CapturingHandler.lock:
            for message in payload.get("messages", []):
                _CapturingHandler.prompts.append(str(message.get("content", "")))
            # Executor prompts carry "Current step:" — route them to the
            # executor queue so worker timing cannot mix the roles.
            if b"Current step:" in raw and _CapturingHandler.executor_queue:
                text = _CapturingHandler.executor_queue.pop(0)
            elif b"Current step:" in raw:
                # the queue can run dry on the closing step, which must still close
                text = CLOSE_OUT_ANSWER
            else:
                text = (
                    _CapturingHandler.queue.pop(0)
                    if _CapturingHandler.queue
                    else PLANNER_ANSWER
                )
        body = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
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
    _CapturingHandler.queue = []
    _CapturingHandler.executor_queue = []
    _CapturingHandler.prompts = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
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
        self.vault = VaultService(
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
        models = ModelService(
            registry, self.vault, self.kernel, self.operations, endpoints,
            recovery_repository,
        )
        self.vault.add("fake-key", "unused-key")
        self.memory = CampaignMemoryService(MemoryRepository(self.database), self.vault)
        self.skills = SkillService(SkillRepository(self.database), self.vault)
        self.playbooks = PlaybookService(
            self.operations, self.kernel, PlaybookRepository(self.database)
        )
        runs = AgentRunRepository(self.database)
        self.orchestrator = AgentOrchestrator(
            models, self.kernel, execution, self.operations, runs,
            memory=self.memory, skills=self.skills,
        )
        self.swarms = SwarmService(
            self.operations, self.kernel, self.orchestrator,
            SwarmRunRepository(self.database),
        )

    def make_operation(self, name: str, tasks: list[PlanTask]) -> tuple[str, str]:
        operation = self.operations.create(
            name, OperationMode.BUG_BOUNTY, ScopeContract(includes=("example.com",))
        )
        self.kernel.create_plan(operation.id, f"{name} plan", tasks, [])
        # scripted answers run no tools; the completion gate wants evidence
        for task in tasks:
            self.kernel.register_artifact(
                operation.id, ArtifactKind.NOTE, "seed", task_id=task.id
            )
        approval = self.kernel.request_approval(
            operation.id, "model.call fake/agent@v1", RiskClass.R2
        )
        self.kernel.approve(approval.id, decided_by="operator")
        return operation.id, approval.id


def test_campaign_memory_shared_across_operations_with_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    stack = Stack(tmp_path, monkeypatch, fake_server)
    stack.vault.add("api-token", "leak-me-never")
    stack.memory.add(
        "acme-campaign",
        "WAF bypass decision",
        "the api-token value leak-me-never was rotated",
        kind=MemoryKind.DECISION,
        tags=("waf",),
    )
    stack.memory.add("acme-campaign", "Recon methodology", "subdomains first")
    stack.memory.add("other-ws", "Unrelated", "not for this campaign")

    task = PlanTask(id="recon", title="Recon", risk_class=RiskClass.R1)
    first_op, first_approval = stack.make_operation("First op", [task])
    second_op, second_approval = stack.make_operation(
        "Second op", [PlanTask(id="recon", title="Recon", risk_class=RiskClass.R1)]
    )
    stack.kernel.start_task(first_op, "recon")
    stack.kernel.start_task(second_op, "recon")

    _CapturingHandler.queue = [PLANNER_ANSWER, EXECUTOR_ANSWER] * 2
    run_a = stack.orchestrator.run_task(
        first_op, "recon", ["fake/agent@v1"],
        approval_id=first_approval, workspace="acme-campaign",
    )
    run_b = stack.orchestrator.run_task(
        second_op, "recon", ["fake/agent@v1"],
        approval_id=second_approval, workspace="acme-campaign",
    )
    assert run_a.status is AgentRunStatus.COMPLETED
    assert run_b.status is AgentRunStatus.COMPLETED

    # обе операции получили digest своего workspace, без чужих записей и секретов
    # a corrective retry repeats the prompt, so match the role explicitly
    planner_prompts = [
        prompt
        for prompt in _CapturingHandler.prompts
        if prompt.startswith(_PLANNER_MARKER)
    ]
    assert len(planner_prompts) == 2
    for prompt in planner_prompts:
        assert "Campaign memory" in prompt
        assert "WAF bypass decision" in prompt
        assert "Recon methodology" in prompt
        assert "Unrelated" not in prompt
        assert "leak-me-never" not in prompt

    # аудит: memory ids зафиксированы в agent.run.start обеих операций
    for operation_id, run in ((first_op, run_a), (second_op, run_b)):
        start = next(
            event
            for event in stack.operations.events(operation_id)
            if event.event_type == "agent.run.start"
            and event.payload["run_id"] == run.id
        )
        assert start.payload["workspace"] == "acme-campaign"
        assert len(start.payload["memory_entry_ids"]) == 2

    # retention и удаление: память — не evidence, удаление разрешено
    expired = stack.memory.add(
        "acme-campaign", "Stale note", retention_days=1
    )
    expired.updated_at = datetime.now(UTC) - timedelta(days=2)
    stack.memory.memory.update(expired, expected_version=1)
    purged = stack.memory.purge_expired()
    assert purged == [expired.id]
    victim = stack.memory.list_entries("acme-campaign")[0]
    stack.memory.delete(victim.id)
    with pytest.raises(MemoryNotFoundError):
        stack.memory.get(victim.id)


def test_custom_playbook_and_trusted_skills_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: str
) -> None:
    stack = Stack(tmp_path, monkeypatch, fake_server)

    # FR-18: skill без trust review отклоняется
    with pytest.raises(SkillTrustError):
        stack.skills.install("untrusted", "do things", trusted=False)
    stack.skills.install(
        "asset-logging", "Log the source of every discovered asset.", trusted=True
    )

    # custom playbook: install → use → governed plan
    template_path = tmp_path / "mini-playbook.json"
    template_path.write_text(
        json.dumps(
            {
                "name": "mini-recon",
                "title": "Mini recon engagement",
                "tasks": [
                    {"id": "map", "title": "Map surface", "risk_class": "r1",
                     "depends_on": [], "required_artifacts": []},
                    {"id": "probe", "title": "Probe endpoints", "risk_class": "r1",
                     "depends_on": [], "required_artifacts": []},
                ],
                "gates": [],
            }
        ),
        encoding="utf-8",
    )
    template = stack.playbooks.install_custom(template_path)
    assert template.name == "mini-recon"

    operation = stack.operations.create(
        "Custom playbook op", OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("example.com",)),
    )
    plan = stack.playbooks.use_custom(operation.id, "mini-recon")
    assert {task.id for task in plan.tasks} == {"map", "probe"}
    assert all(task.status is TaskStatus.PENDING for task in plan.tasks)

    approval = stack.kernel.request_approval(
        operation.id, "model.call fake/agent@v1", RiskClass.R2
    )
    stack.kernel.approve(approval.id, decided_by="operator")

    # the scripted executor never runs a tool, so evidence is seeded per task
    for task in plan.tasks:
        stack.kernel.register_artifact(
            operation.id, ArtifactKind.NOTE, "seed", task_id=task.id
        )
    _CapturingHandler.queue = [PLANNER_ANSWER, PLANNER_ANSWER]
    _CapturingHandler.executor_queue = [EXECUTOR_ANSWER, EXECUTOR_ANSWER]
    swarm = stack.swarms.start(
        operation.id,
        None,
        ["fake/agent@v1"],
        workers=2,
        approval_id=approval.id,
        workspace=None,
        skill_names=("asset-logging",),
    )
    assert swarm.status is SwarmStatus.COMPLETED

    members = stack.swarms.members(swarm.id)
    assert all(member.skills == ("asset-logging",) for member in members)
    assert any(
        "Log the source of every discovered asset" in prompt
        for prompt in _CapturingHandler.prompts
    )
    start = next(
        event
        for event in stack.operations.events(operation.id)
        if event.event_type == "agent.swarm.start"
    )
    assert start.payload["skills"] == ["asset-logging"]

    plan_after = stack.kernel.get_current_plan(operation.id)
    assert plan_after is not None
    assert all(task.status is TaskStatus.DONE for task in plan_after.tasks)

    # переустановка playbook: immutable — дубликат имени отклоняется
    with pytest.raises(PlaybookError):
        stack.playbooks.install_custom(template_path)
