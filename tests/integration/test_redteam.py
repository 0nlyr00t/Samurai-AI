import json
from pathlib import Path

import pytest

from samurai_ai.domain.ctf import ChallengeCategory, CtfModeError
from samurai_ai.domain.kernel import ApprovalStatus, ArtifactKind
from samurai_ai.domain.operations import Operation, OperationMode, OperationStatus
from samurai_ai.domain.redteam import (
    AttackPathError,
    AttackStepStateError,
    AttackStepStatus,
    CheckpointStateError,
    DebriefStateError,
    DetectionStatus,
    RedTeamModeError,
    RoeExistsError,
    RoeNotFoundError,
)
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.kernel import KernelPreconditionError, KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.ctf import CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.redteam import RedTeamRepository
from samurai_ai.storage.vault import VaultRepository


class Stack:
    def __init__(self, database_path: Path, with_vault: bool = False) -> None:
        database = Database(database_path)
        self.operations = OperationService(OperationRepository(database))
        self.kernel = KernelService(self.operations, KernelRepository(database))
        vault = None
        if with_vault:
            vault = VaultService(
                VaultRepository(database),
                self.operations.repository,
                database_path.parent / "vault.key",
            )
        self.vault = vault
        self.redteam = RedTeamService(
            self.operations, self.kernel, RedTeamRepository(database), vault
        )


def make_operation(stack: Stack) -> Operation:
    operation = stack.operations.create(
        "RT engagement",
        OperationMode.RED_TEAM,
        ScopeContract(includes=("corp.local", "*.corp.local")),
    )
    stack.redteam.init_red_team(
        operation.id,
        "APT29-style intrusion",
        ("gain domain admin", "exfiltrate synthetic data"),
        constraints="no destructive actions",
        success_criteria="DA reached with detection notes",
    )
    return operation


def test_init_is_one_shot_and_mode_checked(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    with pytest.raises(RoeExistsError):
        stack.redteam.init_red_team(operation.id, "again", ("x",))

    ctf_op = stack.operations.create(
        "CTF", OperationMode.CTF_HTB, ScopeContract(includes=("10.10.10.0/24",))
    )
    with pytest.raises(RedTeamModeError):
        stack.redteam.init_red_team(ctf_op.id, "nope", ("x",))
    with pytest.raises(RoeNotFoundError):
        stack.redteam.get_roe(ctf_op.id)


def test_init_creates_plan_roe_artifact_and_blocking_approval(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    plan = stack.kernel.kernel.get_current_plan(operation.id)
    assert plan is not None
    assert [task.id for task in plan.tasks] == [
        "roe-review",
        "path-planning",
        "execution",
        "detection-review",
        "debrief",
    ]
    assert stack.kernel.kernel.artifact_exists(operation.id, "roe")

    pending = stack.kernel.kernel.list_approvals(
        operation.id, status=ApprovalStatus.PENDING
    )
    assert len(pending) == 1
    assert pending[0].risk_class.value == "r3"

    # BRD scenario 4: execution cannot start while the ROE approval is pending
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation.id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation.id)
    with pytest.raises(KernelPreconditionError, match="pending approvals"):
        stack.kernel.transition_operation(operation.id, OperationStatus.EXECUTION)
    stack.kernel.approve(pending[0].id, decided_by="operator")
    stack.kernel.transition_operation(operation.id, OperationStatus.EXECUTION)


def test_add_step_validates_dependencies(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    with pytest.raises(AttackPathError, match="not found"):
        stack.redteam.add_step(
            operation.id, "dump", "T1003", depends_on=("step_missing",)
        )
    first = stack.redteam.add_step(operation.id, "phish", "T1566")
    second = stack.redteam.add_step(
        operation.id, "dump creds", "T1003", depends_on=(first.id,)
    )
    assert second.depends_on == (first.id,)


def test_step_transitions_and_detection(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    step = stack.redteam.add_step(operation.id, "phish", "T1566")

    with pytest.raises(AttackStepStateError):
        stack.redteam.update_step(step.id, "complete")
    with pytest.raises(AttackStepStateError):
        stack.redteam.record_detection(step.id, DetectionStatus.MISSED)

    stack.redteam.update_step(step.id, "start")
    updated = stack.redteam.update_step(step.id, "complete", "payload ran")
    assert updated.status is AttackStepStatus.EXECUTED
    detected = stack.redteam.record_detection(
        step.id, DetectionStatus.DETECTED, "EDR alert"
    )
    assert detected.detection is DetectionStatus.DETECTED
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "attack-step.added" in events
    assert "attack-step.executing" in events
    assert "attack-step.executed" in events
    assert "attack-step.detection" in events


def test_attach_step_evidence(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    step = stack.redteam.add_step(operation.id, "exfil", "T1041")
    artifact = stack.kernel.register_artifact(
        operation.id, ArtifactKind.LOG, "pcap excerpt", summary="exfil pcap"
    )
    updated = stack.redteam.attach_step_evidence(step.id, artifact.id)
    assert updated.evidence_ids == (artifact.id,)
    with pytest.raises(AttackPathError, match="not found"):
        stack.redteam.attach_step_evidence(step.id, "art_missing")


def test_checkpoint_flow(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    checkpoint = stack.redteam.add_checkpoint(
        operation.id, "Before DA attempt", "confirm scope window"
    )
    reached = stack.redteam.reach_checkpoint(checkpoint.id, "window confirmed")
    assert reached.status.value == "reached"
    with pytest.raises(CheckpointStateError):
        stack.redteam.reach_checkpoint(checkpoint.id)


def test_attack_path_orders_dependencies_first(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    first = stack.redteam.add_step(operation.id, "phish", "T1566")
    second = stack.redteam.add_step(
        operation.id, "dump", "T1003", depends_on=(first.id,)
    )
    stack.redteam.add_step(
        operation.id, "lateral", "T1021", depends_on=(second.id,)
    )
    path = stack.redteam.attack_path(operation.id)
    assert path.index("phish") < path.index("dump") < path.index("lateral")
    assert "T1003" in path


def test_debrief_requires_status_and_steps(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    with pytest.raises(DebriefStateError, match="verification or reporting"):
        stack.redteam.generate_debrief(operation.id)


def drive_to_verification(stack: Stack, operation_id: str) -> None:
    pending = stack.kernel.kernel.list_approvals(
        operation_id, status=ApprovalStatus.PENDING
    )
    for approval in pending:
        stack.kernel.approve(approval.id, decided_by="operator")
    stack.kernel.transition_operation(operation_id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation_id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.EXECUTION)
    stack.kernel.transition_operation(operation_id, OperationStatus.VERIFICATION)


def test_debrief_content_and_open_gaps(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    done = stack.redteam.add_step(operation.id, "phish", "T1566")
    skipped = stack.redteam.add_step(
        operation.id, "wifi pivot", "T1010", depends_on=(done.id,)
    )
    stack.redteam.update_step(done.id, "start")
    stack.redteam.update_step(done.id, "complete")
    stack.redteam.record_detection(done.id, DetectionStatus.MISSED, "no alert")
    stack.redteam.update_step(skipped.id, "skip", "no wifi in scope")
    stack.redteam.add_checkpoint(operation.id, "mid-op check")
    drive_to_verification(stack, operation.id)

    debrief = stack.redteam.generate_debrief(operation.id)
    text = (tmp_path / debrief.path).read_text(encoding="utf-8")
    assert "Red Team Debrief" in text
    assert "APT29-style intrusion" in text
    assert "T1566" in text
    assert any("missed" in gap for gap in debrief.open_gaps)
    assert any("skipped" in gap for gap in debrief.open_gaps)
    assert stack.kernel.kernel.artifact_exists(operation.id, "final-debrief")
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "debrief.generated" in events


def test_debrief_json_and_read(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    step = stack.redteam.add_step(operation.id, "phish", "T1566")
    stack.redteam.update_step(step.id, "start")
    stack.redteam.update_step(step.id, "complete")
    drive_to_verification(stack, operation.id)

    debrief = stack.redteam.generate_debrief(operation.id, ReportFormat.JSON)
    payload = json.loads((tmp_path / debrief.path).read_text(encoding="utf-8"))
    assert payload["roe"]["threat_profile"] == "APT29-style intrusion"
    assert payload["attack_path"][0]["technique_id"] == "T1566"

    shown = stack.redteam.read_debrief(operation.id, debrief.id)
    assert "T1566" in shown
    assert stack.redteam.list_debriefs(operation.id)[0].id == debrief.id


def test_debrief_redacts_vault_secrets(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db", with_vault=True)
    assert stack.vault is not None
    operation = make_operation(stack)
    stack.vault.add("c2-token", "rt-secret-token-555", operation.id)
    step = stack.redteam.add_step(
        operation.id, "exfil", "T1041", notes="used rt-secret-token-555"
    )
    stack.redteam.update_step(step.id, "start")
    stack.redteam.update_step(step.id, "complete", "channel rt-secret-token-555")
    drive_to_verification(stack, operation.id)

    debrief = stack.redteam.generate_debrief(operation.id)
    text = (tmp_path / debrief.path).read_text(encoding="utf-8")
    assert "rt-secret-token-555" not in text
    assert "***REDACTED***" in text


def test_export_debrief_manifest_with_evidence(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    step = stack.redteam.add_step(operation.id, "phish", "T1566")
    artifact = stack.kernel.register_artifact(
        operation.id, ArtifactKind.LOG, "mail log", summary="phish log"
    )
    stack.redteam.attach_step_evidence(step.id, artifact.id)
    stack.redteam.update_step(step.id, "start")
    stack.redteam.update_step(step.id, "complete")
    drive_to_verification(stack, operation.id)
    debrief = stack.redteam.generate_debrief(operation.id)

    dest = tmp_path / "export"
    manifest = stack.redteam.export_debrief(operation.id, debrief.id, dest)
    assert (dest / "debrief.md").exists()
    assert (dest / "manifest.json").exists()
    paths = {entry.path for entry in manifest.files}
    assert "debrief.md" in paths
    assert any(path.startswith("artifacts/") for path in paths)
    payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert payload["report_id"] == debrief.id


def test_summary_counts(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    first = stack.redteam.add_step(operation.id, "phish", "T1566")
    stack.redteam.add_step(operation.id, "dump", "T1003")
    stack.redteam.update_step(first.id, "start")
    stack.redteam.update_step(first.id, "complete")
    stack.redteam.record_detection(first.id, DetectionStatus.DETECTED)
    checkpoint = stack.redteam.add_checkpoint(operation.id, "mid")
    stack.redteam.reach_checkpoint(checkpoint.id)

    summary = stack.redteam.summary(operation.id)
    assert summary.steps_planned == 1
    assert summary.steps_executed == 1
    assert summary.detections_detected == 1
    assert summary.detections_missed == 0
    assert summary.checkpoints_reached == 1
    assert summary.checkpoints_total == 1
    assert summary.debriefs == 0


def test_ctf_service_still_rejects_red_team_mode(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    ctf = CtfService(stack.operations, stack.kernel, CtfRepository(stack.redteam.redteam.database))
    operation = make_operation(stack)
    with pytest.raises(CtfModeError):
        ctf.init_ctf(operation.id, "X", ChallengeCategory.WEB)
