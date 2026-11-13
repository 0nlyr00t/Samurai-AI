"""The orchestrator injects active learned skills and closes the loop on finish."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_agents import (  # type: ignore[import-not-found]
    MODEL_ID,
    AgentRunStatus,
    make_operation,
    make_plan,
    make_stack,
)

from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity
from samurai_ai.domain.learned_skills import (
    LearnedSkill,
    LearnedSkillMode,
    LearnedSkillStatus,
)
from samurai_ai.services.learned_skills import LearnedSkillService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.settings import SettingsService
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.learned_skills import LearnedSkillRepository
from samurai_ai.storage.memory import MemoryRepository, MemorySearchRepository
from samurai_ai.storage.recovery import RecoveryRepository

WORKSPACE = "lab"


def _wire_learning(orchestrator, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Attach learned-skills + memory (the orchestrator needs memory for a workspace)."""
    database = orchestrator.operations.repository.database
    settings = SettingsService(RecoveryRepository(database))
    tool_runs = ToolRunRepository(database)
    learned = LearnedSkillService(
        orchestrator.operations,
        orchestrator.kernel,
        tool_runs,
        LearnedSkillRepository(database),
        settings,
    )
    orchestrator.learned_skills = learned
    orchestrator.memory = CampaignMemoryService(
        MemoryRepository(database), search=MemorySearchRepository(database)
    )
    return learned, tool_runs


def _confirmed(kernel, operation_id: str, title: str = "SMB null session") -> None:  # type: ignore[no-untyped-def]
    artifact = kernel.register_artifact(operation_id, ArtifactKind.COMMAND_OUTPUT, "body")
    finding = kernel.create_finding(operation_id, title, FindingSeverity.MEDIUM, "anon read")
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.validate_finding(finding.id)


def _scripted_complete(fake) -> None:  # type: ignore[no-untyped-def]
    fake.queue(
        '{"steps": ["probe"]}',
        '{"commands": [{"type": "complete_task", "no_finding": "scripted"}]}',
    )


def test_active_learned_skill_reaches_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _models, orchestrator, _fake = make_stack(tmp_path, monkeypatch)
    learned, _tool_runs = _wire_learning(orchestrator, tmp_path)
    learned.repo.add(
        LearnedSkill(
            workspace=WORKSPACE,
            title="map smb",
            cue="lab: smb enumeration",
            body="Steps that worked:\n1. enum4linux -a host",
            status=LearnedSkillStatus.ACTIVE,
            source_operation_id="op_seed",
            evidence=("find_seed",),
            sha256="e" * 64,
        )
    )
    learned.set_mode(LearnedSkillMode.INJECT)

    context, _mem_ids, _skill_ids, learned_ids = orchestrator._resolve_context(
        WORKSPACE, [], "enumerate smb shares"
    )

    assert "Learned skills (agent-distilled, unverified" in context
    assert "enum4linux -a host" in context
    assert len(learned_ids) == 1


def test_a_finished_run_distils_a_skill_from_a_confirmed_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    learned, tool_runs = _wire_learning(orchestrator, tmp_path)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    _confirmed(kernel, operation_id)
    # a completed command in this operation becomes the procedure
    from samurai_ai.domain.execution import ToolRun, ToolRunStatus
    from samurai_ai.domain.operations import OperationEvent

    tool_runs.add_run(
        ToolRun(
            operation_id=operation_id,
            task_id="probe",
            tool_id="shell.exec",
            tool_version=1,
            parameters={"command": "enum4linux -a host"},
            status=ToolRunStatus.COMPLETED,
        ),
        OperationEvent(operation_id=operation_id, event_type="tool.run.completed"),
    )
    _scripted_complete(fake)

    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID], workspace=WORKSPACE)

    assert run.status is AgentRunStatus.COMPLETED
    skills = learned.all(WORKSPACE)
    assert len(skills) == 1
    assert skills[0].status is LearnedSkillStatus.SHADOW
    assert "enum4linux -a host" in skills[0].body
    types = [event.event_type for event in service.events(operation_id)]
    assert "learned_skill.extracted" in types


def test_learning_never_breaks_a_finished_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, kernel, _models, orchestrator, fake = make_stack(tmp_path, monkeypatch)
    learned, _tool_runs = _wire_learning(orchestrator, tmp_path)
    operation_id = make_operation(service)
    make_plan(kernel, operation_id)
    _scripted_complete(fake)

    # no confirmed finding → extract is a no-op, run still completes cleanly
    run = orchestrator.run_task(operation_id, "probe", [MODEL_ID], workspace=WORKSPACE)

    assert run.status is AgentRunStatus.COMPLETED
    assert learned.all(WORKSPACE) == []
