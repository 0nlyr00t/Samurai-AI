"""Learned-skills service: distil deterministically, gate, inject, accrue fitness."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.execution import ToolRun, ToolRunStatus
from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity, PlanTask
from samurai_ai.domain.learned_skills import (
    LearnedSkillGateError,
    LearnedSkillMode,
    LearnedSkillStatus,
)
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.learned_skills import LearnedSkillService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.learned_skills import LearnedSkillRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.vault import VaultRepository

WORKSPACE = "lab"
TEST_KEY = "cd" * 32


@pytest.fixture
def stack(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "learned.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    settings = SettingsService(RecoveryRepository(database))
    tool_runs = ToolRunRepository(database)
    learned = LearnedSkillService(
        operations, kernel, tool_runs, LearnedSkillRepository(database), settings
    )
    operation = operations.create("lab", OperationMode.CTF_HTB, ScopeContract(includes=("x",)))
    kernel.create_plan(operation.id, "plan", [PlanTask(id="probe", title="Probe")], [])
    return learned, kernel, operations, tool_runs, operation.id


def _confirmed(kernel: KernelService, operation_id: str, title: str = "SMB null session") -> str:
    artifact = kernel.register_artifact(operation_id, ArtifactKind.COMMAND_OUTPUT, "body")
    finding = kernel.create_finding(operation_id, title, FindingSeverity.MEDIUM, "anon read")
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.validate_finding(finding.id)
    return finding.id


def _command(tool_runs: ToolRunRepository, operation_id: str, command: str,
             status: ToolRunStatus = ToolRunStatus.COMPLETED) -> None:
    run = ToolRun(
        operation_id=operation_id,
        task_id="probe",
        tool_id="shell.exec",
        tool_version=1,
        parameters={"command": command},
        status=status,
    )
    tool_runs.add_run(
        run, OperationEvent(operation_id=operation_id, event_type="tool.run.completed")
    )


def test_a_confirmed_episode_distils_one_skill(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    finding_id = _confirmed(kernel, operation_id)
    _command(tool_runs, operation_id, "enum4linux -a 10.0.0.5")

    admitted = learned.extract(operation_id, WORKSPACE)

    assert len(admitted) == 1
    skill = admitted[0]
    assert skill.status is LearnedSkillStatus.SHADOW
    assert skill.evidence == (finding_id,)
    assert "enum4linux -a 10.0.0.5" in skill.body
    assert "SMB null session" in skill.body


def test_an_episode_without_a_confirmed_finding_teaches_nothing(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    kernel.create_finding(operation_id, "unverified", FindingSeverity.INFO)
    _command(tool_runs, operation_id, "nmap 10.0.0.5")

    assert learned.extract(operation_id, WORKSPACE) == []
    assert learned.all(WORKSPACE) == []


def test_failed_commands_become_pitfalls(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    _confirmed(kernel, operation_id)
    _command(tool_runs, operation_id, "hydra -l x ssh://host", ToolRunStatus.FAILED)
    _command(tool_runs, operation_id, "enum4linux -a host")

    skill = learned.extract(operation_id, WORKSPACE)[0]

    assert "Pitfalls:" in skill.body
    assert "hydra -l x ssh://host" in skill.body


def test_extraction_is_idempotent(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    _confirmed(kernel, operation_id)
    _command(tool_runs, operation_id, "enum4linux -a host")

    first = learned.extract(operation_id, WORKSPACE)
    learned.extract(operation_id, WORKSPACE)

    stored = learned.all(WORKSPACE)
    assert len(stored) == 1
    assert stored[0].id == first[0].id


def test_secrets_are_redacted_from_the_body(tmp_path: Path) -> None:
    database = Database(tmp_path / "redact.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    settings = SettingsService(RecoveryRepository(database))
    tool_runs = ToolRunRepository(database)
    vault = VaultService(VaultRepository(database), operations.repository, tmp_path / "v.key")
    learned = LearnedSkillService(
        operations, kernel, tool_runs, LearnedSkillRepository(database), settings, vault
    )
    operation = operations.create("lab", OperationMode.CTF_HTB, ScopeContract(includes=("x",)))
    kernel.create_plan(operation.id, "plan", [PlanTask(id="probe", title="Probe")], [])
    vault.add("api-token", "s3cr3t-token-value")
    _confirmed(kernel, operation.id)
    _command(tool_runs, operation.id, "curl -H 'Authorization: s3cr3t-token-value' host")

    skill = learned.extract(operation.id, WORKSPACE)[0]

    assert "s3cr3t-token-value" not in skill.body


def test_prompt_fragment_is_empty_unless_mode_is_inject(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    _confirmed(kernel, operation_id)
    _command(tool_runs, operation_id, "enum4linux -a host")
    skill = learned.extract(operation_id, WORKSPACE)[0]
    learned.promote(skill.id)

    assert learned.mode() is LearnedSkillMode.SHADOW
    assert learned.prompt_fragment(WORKSPACE) == ("", ())

    learned.set_mode(LearnedSkillMode.INJECT)
    fragment, ids = learned.prompt_fragment(WORKSPACE, "smb enumeration")
    assert "Learned skill" in fragment
    assert ids == (skill.id,)


def test_only_active_skills_are_injected(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    _confirmed(kernel, operation_id)
    _command(tool_runs, operation_id, "enum4linux -a host")
    learned.extract(operation_id, WORKSPACE)  # stays shadow
    learned.set_mode(LearnedSkillMode.INJECT)

    assert learned.prompt_fragment(WORKSPACE) == ("", ())


def test_promote_without_evidence_is_refused(stack) -> None:
    learned, _kernel, operations, _tool_runs, operation_id = stack
    # a skill with empty evidence cannot exist via extract, so forge one directly
    from samurai_ai.domain.learned_skills import LearnedSkill, content_sha

    repo = learned.repo
    sha = content_sha(WORKSPACE, "cue", "body")
    repo.add(
        LearnedSkill(
            workspace=WORKSPACE,
            title="t",
            cue="cue",
            body="body",
            source_operation_id=operation_id,
            evidence=(),
            sha256=sha,
        )
    )
    stored = learned.all(WORKSPACE)[0]
    with pytest.raises(LearnedSkillGateError):
        learned.promote(stored.id)


def test_reuse_outcome_accrues_fitness(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    _confirmed(kernel, operation_id)
    _command(tool_runs, operation_id, "enum4linux -a host")
    skill = learned.extract(operation_id, WORKSPACE)[0]

    learned.record_reuse_outcome([skill.id], produced_finding=True, invalid_actions=2)
    learned.record_reuse_outcome([skill.id], produced_finding=False, invalid_actions=0)

    refreshed = learned.get(skill.id)
    assert refreshed.fitness.reuses == 2
    assert refreshed.fitness.findings_after_reuse == 1
    assert refreshed.fitness.median_invalid_actions == pytest.approx(1.0)


def test_curate_retires_an_active_skill_that_never_helped(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    _confirmed(kernel, operation_id)
    _command(tool_runs, operation_id, "enum4linux -a host")
    skill = learned.extract(operation_id, WORKSPACE)[0]
    learned.promote(skill.id)
    for _ in range(3):
        learned.record_reuse_outcome([skill.id], produced_finding=False, invalid_actions=1)

    assert learned.curate(WORKSPACE) == 1
    assert learned.get(skill.id).status is LearnedSkillStatus.RETIRED


def test_skills_are_workspace_scoped(stack) -> None:
    learned, kernel, _ops, tool_runs, operation_id = stack
    _confirmed(kernel, operation_id)
    _command(tool_runs, operation_id, "enum4linux -a host")
    learned.extract(operation_id, WORKSPACE)

    assert learned.all("other") == []
