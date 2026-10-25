"""Stage 4c acceptance criteria as executable tests (Red Team playbook).

BRD scenario 4: a red team scenario carries an ROE, ATT&CK mapping, success
criteria, stop checkpoints and detection expectations; execution starts only
after approval. The operation ends with a debrief that lists evidence and
open gaps, never leaks vault secrets, and survives a database reopen.
"""

import hashlib
import json
from pathlib import Path

import pytest

from samurai_ai.domain.kernel import ApprovalStatus, ArtifactKind
from samurai_ai.domain.operations import OperationMode, OperationStatus
from samurai_ai.domain.redteam import (
    DebriefStateError,
    DetectionStatus,
    RedTeamModeError,
    RoeExistsError,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelPreconditionError, KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.redteam import RedTeamRepository
from samurai_ai.storage.vault import VaultRepository


class Stack:
    def __init__(self, database_path: Path) -> None:
        database = Database(database_path)
        self.operations = OperationService(OperationRepository(database))
        self.kernel = KernelService(self.operations, KernelRepository(database))
        self.vault = VaultService(
            VaultRepository(database),
            self.operations.repository,
            database_path.parent / "vault.key",
        )
        self.redteam = RedTeamService(
            self.operations, self.kernel, RedTeamRepository(database), self.vault
        )


def test_stage4c_red_team_acceptance(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    stack = Stack(database_path)
    operation = stack.operations.create(
        "Acceptance RT",
        OperationMode.RED_TEAM,
        ScopeContract(includes=("corp.local", "*.corp.local")),
        objective="Emulate an APT29-style intrusion",
    )
    operation_id = operation.id

    # --- ROE init: document, template plan, roe artifact, blocking approval ---
    plan = stack.redteam.init_red_team(
        operation_id,
        "APT29-style intrusion",
        ("gain domain admin", "exfiltrate synthetic data"),
        constraints="no destructive actions; window 09:00-18:00",
        success_criteria="DA reached; detection coverage assessed",
    )
    assert [task.id for task in plan.tasks] == [
        "roe-review",
        "path-planning",
        "execution",
        "detection-review",
        "debrief",
    ]
    assert stack.kernel.kernel.artifact_exists(operation_id, "roe")

    # --- execution is impossible before the ROE approval (BRD scenario 4) ---
    stack.kernel.transition_operation(operation_id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation_id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation_id)
    with pytest.raises(KernelPreconditionError, match="pending approvals"):
        stack.kernel.transition_operation(operation_id, OperationStatus.EXECUTION)
    pending = stack.kernel.kernel.list_approvals(
        operation_id, status=ApprovalStatus.PENDING
    )
    assert len(pending) == 1
    stack.kernel.approve(pending[0].id, decided_by="operator")
    stack.kernel.transition_operation(operation_id, OperationStatus.EXECUTION)

    # --- governed tasks and attack path with ATT&CK mapping ---
    stack.kernel.start_task(operation_id, "roe-review")
    stack.kernel.complete_task(operation_id, "roe-review")
    stack.kernel.start_task(operation_id, "path-planning")

    phish = stack.redteam.add_step(
        operation_id,
        "spearphish with attachment",
        "T1566.001",
        expected_telemetry="process creation from winword",
        detection_criteria="email gateway + EDR process alert",
    )
    dump = stack.redteam.add_step(
        operation_id,
        "dump LSASS",
        "T1003.001",
        expected_telemetry="lsass process access",
        detection_criteria="Sysmon EID 10",
        depends_on=(phish.id,),
    )
    pivot = stack.redteam.add_step(
        operation_id,
        "wifi pivot",
        "T1010",
        depends_on=(dump.id,),
    )
    stack.redteam.add_checkpoint(
        operation_id, "Before credential theft", "confirm scope window"
    )
    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.NOTE,
        "attack path with 3 steps",
        requirement="attack-path",
        task_id="execution",
        gate_id="path-approved",
    )
    stack.kernel.complete_task(operation_id, "path-planning")
    stack.kernel.start_task(operation_id, "execution")

    checkpoint = stack.redteam.list_checkpoints(operation_id)[0]
    stack.redteam.reach_checkpoint(checkpoint.id, "window confirmed")

    phish_evidence = stack.kernel.register_artifact(
        operation_id, ArtifactKind.LOG, "gateway log excerpt", summary="phish log"
    )
    stack.redteam.attach_step_evidence(phish.id, phish_evidence.id)
    stack.redteam.update_step(phish.id, "start")
    stack.redteam.update_step(phish.id, "complete", "payload executed")
    stack.redteam.record_detection(
        phish.id, DetectionStatus.DETECTED, "EDR process alert fired"
    )
    stack.redteam.update_step(dump.id, "start")
    stack.redteam.update_step(dump.id, "complete", "dumped")
    stack.redteam.record_detection(dump.id, DetectionStatus.MISSED, "no Sysmon 10")
    stack.redteam.update_step(pivot.id, "skip", "no wifi in scope")

    # a secret that must never leak into the debrief
    stack.vault.add("c2-token", "rt-secret-token-999", operation_id)
    sneaky = stack.redteam.add_step(
        operation_id, "exfil over https", "T1041", depends_on=(dump.id,)
    )
    stack.redteam.update_step(sneaky.id, "start")
    stack.redteam.update_step(sneaky.id, "complete", "channel rt-secret-token-999")

    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.LOG,
        "execution log",
        requirement="execution-log",
        task_id="detection-review",
        gate_id="execution-finished",
    )
    stack.kernel.complete_task(operation_id, "execution")
    stack.kernel.start_task(operation_id, "detection-review")
    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.NOTE,
        "detection outcomes reviewed",
        requirement="detection-log",
        task_id="detection-review",
    )
    stack.kernel.complete_task(operation_id, "detection-review")

    # --- verification: debrief possible, reporting blocked by gates ---
    stack.kernel.transition_operation(operation_id, OperationStatus.VERIFICATION)
    with pytest.raises(KernelPreconditionError, match="gates not passed"):
        stack.kernel.transition_operation(operation_id, OperationStatus.REPORTING)

    debrief = stack.redteam.generate_debrief(operation_id)
    text = (tmp_path / debrief.path).read_text(encoding="utf-8")
    assert "Red Team Debrief" in text
    assert "APT29-style intrusion" in text
    assert "T1566.001" in text
    assert "T1003.001" in text
    # open gaps: missed detection on LSASS dump + skipped wifi pivot
    assert any("T1003.001" in gap and "missed" in gap for gap in debrief.open_gaps)
    assert any("wifi pivot" in gap and "skipped" in gap for gap in debrief.open_gaps)
    # secrets never leak
    assert "rt-secret-token-999" not in text
    assert "***REDACTED***" in text

    stack.kernel.start_task(operation_id, "debrief")
    stack.kernel.complete_task(operation_id, "debrief")
    for gate_id in (
        "roe-approved",
        "path-approved",
        "execution-finished",
        "debrief-ready",
    ):
        stack.kernel.pass_gate(operation_id, gate_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.REPORTING)

    # --- debrief export with manifest ---
    dest = tmp_path / "package"
    manifest = stack.redteam.export_debrief(operation_id, debrief.id, dest)
    assert (dest / "debrief.md").exists()
    assert manifest.report_id == debrief.id
    manifest_payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest_payload["files"]:
        data = (dest / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert any(
        entry["path"].startswith("artifacts/") for entry in manifest_payload["files"]
    )

    stack.kernel.transition_operation(operation_id, OperationStatus.COMPLETE)

    # --- journal carries the full governed trail ---
    event_types = [event.event_type for event in stack.operations.events(operation_id)]
    for expected in (
        "roe.set",
        "attack-step.added",
        "attack-step.executing",
        "attack-step.executed",
        "attack-step.skipped",
        "attack-step.detection",
        "attack-step.evidence",
        "checkpoint.added",
        "checkpoint.reached",
        "debrief.generated",
        "debrief.exported",
    ):
        assert expected in event_types, expected

    # --- everything survives a reopen ---
    reopened = Stack(database_path)
    restored = reopened.operations.get(operation_id)
    assert restored.status is OperationStatus.COMPLETE
    roe = reopened.redteam.get_roe(operation_id)
    assert roe.threat_profile == "APT29-style intrusion"
    steps = reopened.redteam.list_steps(operation_id)
    assert len(steps) == 4
    assert reopened.redteam.list_checkpoints(operation_id)[0].status.value == "reached"
    restored_debrief = reopened.redteam.redteam.get_debrief(debrief.id)
    assert restored_debrief is not None
    assert restored_debrief.sha256 == debrief.sha256
    assert "T1003.001" in reopened.redteam.attack_path(operation_id)


def test_stage4c_guards(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = stack.operations.create(
        "Guarded RT",
        OperationMode.RED_TEAM,
        ScopeContract(includes=("corp.local",)),
    )
    stack.redteam.init_red_team(operation.id, "profile", ("objective",))

    # one-shot guard
    with pytest.raises(RoeExistsError):
        stack.redteam.init_red_team(operation.id, "again", ("x",))

    # debrief requires verification/reporting and at least one step
    with pytest.raises(DebriefStateError, match="verification or reporting"):
        stack.redteam.generate_debrief(operation.id)

    # red team init is rejected on a ctf-htb operation
    ctf_op = stack.operations.create(
        "Wrong mode",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("10.10.10.0/24",)),
    )
    with pytest.raises(RedTeamModeError):
        stack.redteam.init_red_team(ctf_op.id, "nope", ("x",))
