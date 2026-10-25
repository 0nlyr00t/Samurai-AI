from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.kernel import (
    ArtifactKind,
    Finding,
    FindingStatus,
    OperationPlan,
    RiskClass,
)
from samurai_ai.domain.operations import (
    Operation,
    OperationEvent,
    OperationMode,
    OperationStatus,
)
from samurai_ai.domain.redteam import (
    AttackPathError,
    AttackStep,
    AttackStepNotFoundError,
    AttackStepStatus,
    Checkpoint,
    CheckpointNotFoundError,
    CheckpointStatus,
    Debrief,
    DebriefNotFoundError,
    DebriefStateError,
    DetectionStatus,
    RedTeamModeError,
    RoeDocument,
    RoeExistsError,
    RoeNotFoundError,
    red_team_template,
)
from samurai_ai.domain.reports import ManifestFile, ReportFormat, ReportManifest
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.redteam import RedTeamRepository

_ALLOWED_DEBRIEF_STATUSES = (OperationStatus.VERIFICATION, OperationStatus.REPORTING)

StepAction = Literal["start", "complete", "block", "skip"]


class RedTeamSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    steps_planned: int
    steps_executing: int
    steps_executed: int
    steps_blocked: int
    steps_skipped: int
    detections_detected: int
    detections_missed: int
    checkpoints_reached: int
    checkpoints_total: int
    debriefs: int


class RedTeamService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        redteam: RedTeamRepository,
        vault: VaultService | None = None,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.redteam = redteam
        self.vault = vault

    @property
    def debrief_root(self) -> Path:
        return self.redteam.database.path.parent / "reports"

    def init_red_team(
        self,
        operation_id: str,
        threat_profile: str,
        objectives: tuple[str, ...],
        constraints: str = "",
        success_criteria: str = "",
    ) -> OperationPlan:
        operation = self.operations.get(operation_id)
        if operation.mode is not OperationMode.RED_TEAM:
            raise RedTeamModeError(
                f"operation {operation_id} is in {operation.mode.value} mode, "
                "not red-team"
            )
        if self.redteam.get_roe(operation_id) is not None:
            raise RoeExistsError(
                f"operation {operation_id} already has an ROE; "
                "playbook init is one-shot"
            )
        roe = RoeDocument(
            operation_id=operation_id,
            threat_profile=threat_profile,
            objectives=objectives,
            constraints=constraints,
            success_criteria=success_criteria,
        )
        roe_event = OperationEvent(
            operation_id=operation_id,
            event_type="roe.set",
            payload={
                "playbook": "red-team",
                "threat_profile": roe.threat_profile,
                "objectives": list(roe.objectives),
            },
        )
        self.redteam.save_roe(roe, None, roe_event)
        template = red_team_template()
        plan = self.kernel.create_plan(
            operation_id, template.title, template.tasks, template.gates
        )
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.NOTE,
            roe.describe(),
            summary="Rules of engagement",
            requirement="roe",
            task_id="path-planning",
            gate_id="roe-approved",
            created_by="operator",
        )
        self.kernel.request_approval(
            operation_id,
            "Approve ROE and attack path",
            RiskClass.R3,
            reason=f"threat profile: {roe.threat_profile}",
            requested_by="operator",
        )
        return plan

    def get_roe(self, operation_id: str) -> RoeDocument:
        self.operations.get(operation_id)
        roe = self.redteam.get_roe(operation_id)
        if roe is None:
            raise RoeNotFoundError(
                f"operation {operation_id} has no ROE; run roe init first"
            )
        return roe

    def add_step(
        self,
        operation_id: str,
        name: str,
        technique_id: str,
        expected_telemetry: str = "",
        detection_criteria: str = "",
        depends_on: tuple[str, ...] = (),
        notes: str = "",
    ) -> AttackStep:
        self.operations.get(operation_id)
        existing = {step.id for step in self.redteam.list_steps(operation_id)}
        unknown = [dependency for dependency in depends_on if dependency not in existing]
        if unknown:
            raise AttackPathError(
                f"attack step dependencies not found: {', '.join(unknown)}"
            )
        step = AttackStep(
            operation_id=operation_id,
            name=name,
            technique_id=technique_id,
            expected_telemetry=expected_telemetry,
            detection_criteria=detection_criteria,
            depends_on=depends_on,
            notes=notes,
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="attack-step.added",
            payload={
                "step_id": step.id,
                "name": name,
                "technique_id": technique_id,
                "depends_on": list(step.depends_on),
            },
        )
        self.redteam.add_step(step, event)
        return step

    def update_step(
        self, step_id: str, action: StepAction, notes: str = ""
    ) -> AttackStep:
        step = self.redteam.get_step(step_id)
        if step is None:
            raise AttackStepNotFoundError(f"attack step {step_id} not found")
        self.operations.get(step.operation_id)
        expected_version = step.version
        if action == "start":
            step.begin_execution()
            suffix = "executing"
        elif action == "complete":
            step.complete(notes)
            suffix = "executed"
        elif action == "block":
            step.block(notes)
            suffix = "blocked"
        else:
            step.skip(notes)
            suffix = "skipped"
        event = OperationEvent(
            operation_id=step.operation_id,
            event_type=f"attack-step.{suffix}",
            payload={"step_id": step.id, "notes": notes},
        )
        self.redteam.update_step(step, expected_version, event)
        return step

    def record_detection(
        self, step_id: str, status: DetectionStatus, notes: str = ""
    ) -> AttackStep:
        step = self.redteam.get_step(step_id)
        if step is None:
            raise AttackStepNotFoundError(f"attack step {step_id} not found")
        self.operations.get(step.operation_id)
        expected_version = step.version
        step.record_detection(status, notes)
        event = OperationEvent(
            operation_id=step.operation_id,
            event_type="attack-step.detection",
            payload={"step_id": step.id, "detection": status.value, "notes": notes},
        )
        self.redteam.update_step(step, expected_version, event)
        return step

    def attach_step_evidence(self, step_id: str, artifact_id: str) -> AttackStep:
        step = self.redteam.get_step(step_id)
        if step is None:
            raise AttackStepNotFoundError(f"attack step {step_id} not found")
        artifact = self.kernel.kernel.get_artifact(artifact_id)
        if artifact is None or artifact.operation_id != step.operation_id:
            raise AttackPathError(
                f"evidence artifact {artifact_id} not found "
                f"in operation {step.operation_id}"
            )
        expected_version = step.version
        step.attach_evidence(artifact_id)
        event = OperationEvent(
            operation_id=step.operation_id,
            event_type="attack-step.evidence",
            payload={"step_id": step.id, "artifact_id": artifact_id},
        )
        self.redteam.update_step(step, expected_version, event)
        return step

    def list_steps(
        self, operation_id: str, status: AttackStepStatus | None = None
    ) -> list[AttackStep]:
        self.operations.get(operation_id)
        steps = self.redteam.list_steps(operation_id)
        if status is None:
            return steps
        return [step for step in steps if step.status is status]

    def add_checkpoint(
        self, operation_id: str, name: str, criterion: str = ""
    ) -> Checkpoint:
        self.operations.get(operation_id)
        checkpoint = Checkpoint(
            operation_id=operation_id, name=name, criterion=criterion
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="checkpoint.added",
            payload={"checkpoint_id": checkpoint.id, "name": name},
        )
        self.redteam.add_checkpoint(checkpoint, event)
        return checkpoint

    def reach_checkpoint(self, checkpoint_id: str, notes: str = "") -> Checkpoint:
        checkpoint = self.redteam.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            raise CheckpointNotFoundError(f"checkpoint {checkpoint_id} not found")
        self.operations.get(checkpoint.operation_id)
        expected_version = checkpoint.version
        checkpoint.reach(notes)
        event = OperationEvent(
            operation_id=checkpoint.operation_id,
            event_type="checkpoint.reached",
            payload={"checkpoint_id": checkpoint.id, "notes": notes},
        )
        self.redteam.update_checkpoint(checkpoint, expected_version, event)
        return checkpoint

    def list_checkpoints(self, operation_id: str) -> list[Checkpoint]:
        self.operations.get(operation_id)
        return self.redteam.list_checkpoints(operation_id)

    def attack_path(self, operation_id: str) -> str:
        operation = self.operations.get(operation_id)
        steps = self._topological_steps(operation_id)
        lines = [f"# Attack Path: {operation.name}", ""]
        if not steps:
            lines.append("(no attack steps planned)")
        for index, step in enumerate(steps, start=1):
            lines.append(
                f"{index}. [{step.status.value}] {step.name} "
                f"({step.technique_id}) — detection: {step.detection.value}"
            )
            if step.depends_on:
                lines.append(f"   depends on: {', '.join(step.depends_on)}")
            if step.expected_telemetry:
                lines.append(f"   expected telemetry: {step.expected_telemetry}")
        lines.append("")
        return "\n".join(lines)

    def summary(self, operation_id: str) -> RedTeamSummary:
        self.operations.get(operation_id)
        steps = self.redteam.list_steps(operation_id)
        checkpoints = self.redteam.list_checkpoints(operation_id)

        def steps_in(status: AttackStepStatus) -> int:
            return sum(1 for step in steps if step.status is status)

        return RedTeamSummary(
            operation_id=operation_id,
            steps_planned=steps_in(AttackStepStatus.PLANNED),
            steps_executing=steps_in(AttackStepStatus.EXECUTING),
            steps_executed=steps_in(AttackStepStatus.EXECUTED),
            steps_blocked=steps_in(AttackStepStatus.BLOCKED),
            steps_skipped=steps_in(AttackStepStatus.SKIPPED),
            detections_detected=sum(
                1 for step in steps if step.detection is DetectionStatus.DETECTED
            ),
            detections_missed=sum(
                1 for step in steps if step.detection is DetectionStatus.MISSED
            ),
            checkpoints_reached=sum(
                1 for item in checkpoints if item.status is CheckpointStatus.REACHED
            ),
            checkpoints_total=len(checkpoints),
            debriefs=len(self.redteam.list_debriefs(operation_id)),
        )

    def generate_debrief(
        self,
        operation_id: str,
        format: ReportFormat = ReportFormat.MARKDOWN,
        created_by: str = "operator",
    ) -> Debrief:
        operation = self.operations.get(operation_id)
        if operation.status not in _ALLOWED_DEBRIEF_STATUSES:
            raise DebriefStateError(
                f"operation {operation_id} is in {operation.status.value}; "
                "debriefs can be generated in verification or reporting"
            )
        roe = self.get_roe(operation_id)
        steps = self._topological_steps(operation_id)
        if not steps:
            raise DebriefStateError(
                f"operation {operation_id} has no attack steps to debrief"
            )
        checkpoints = self.redteam.list_checkpoints(operation_id)
        findings = self.kernel.kernel.list_findings(
            operation_id, status=FindingStatus.VALIDATED
        )
        open_gaps = tuple(self._open_gaps(steps))
        if format is ReportFormat.JSON:
            content = self._render_json(
                operation, roe, steps, checkpoints, findings, open_gaps
            )
        else:
            content = self._render_markdown(
                operation, roe, steps, checkpoints, findings, open_gaps
            )
        redacted = self._build_redactor(operation_id).redact(content).text

        data = redacted.encode("utf-8")
        suffix = "json" if format is ReportFormat.JSON else "md"
        debrief_id = f"deb_{uuid4().hex[:12]}"
        debrief = Debrief(
            id=debrief_id,
            operation_id=operation_id,
            format=format,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            path=f"reports/{operation_id}/{debrief_id}.{suffix}",
            step_count=len(steps),
            open_gaps=open_gaps,
            created_by=created_by,
        )
        directory = self.debrief_root / operation_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{debrief.id}.{suffix}").write_bytes(data)
        event = OperationEvent(
            operation_id=operation_id,
            event_type="debrief.generated",
            payload={
                "debrief_id": debrief.id,
                "format": format.value,
                "steps": len(steps),
                "open_gaps": len(open_gaps),
            },
        )
        self.redteam.add_debrief(debrief, event)
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.FILE,
            redacted,
            summary=f"Red team debrief {debrief.id}",
            requirement="final-debrief",
            task_id="debrief",
            gate_id="debrief-ready",
            created_by=created_by,
        )
        return debrief

    def list_debriefs(self, operation_id: str) -> list[Debrief]:
        self.operations.get(operation_id)
        return self.redteam.list_debriefs(operation_id)

    def read_debrief(self, operation_id: str, debrief_id: str) -> str:
        self.operations.get(operation_id)
        debrief = self.redteam.get_debrief(debrief_id)
        if debrief is None or debrief.operation_id != operation_id:
            raise DebriefNotFoundError(
                f"debrief {debrief_id} not found in operation {operation_id}"
            )
        path = self.debrief_root.parent / debrief.path
        return path.read_text(encoding="utf-8")

    def export_debrief(
        self, operation_id: str, debrief_id: str, dest_dir: Path
    ) -> ReportManifest:
        self.operations.get(operation_id)
        debrief = self.redteam.get_debrief(debrief_id)
        if debrief is None or debrief.operation_id != operation_id:
            raise DebriefNotFoundError(
                f"debrief {debrief_id} not found in operation {operation_id}"
            )
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        files: list[ManifestFile] = []

        suffix = "json" if debrief.format is ReportFormat.JSON else "md"
        debrief_target = dest / f"debrief.{suffix}"
        shutil.copyfile(self.debrief_root.parent / debrief.path, debrief_target)
        files.append(self._manifest_entry(debrief_target, debrief_target.name))

        artifacts_dir = dest / "artifacts"
        seen: set[str] = set()
        for step in self.redteam.list_steps(operation_id):
            for evidence_id in step.evidence_ids:
                if evidence_id in seen:
                    continue
                seen.add(evidence_id)
                artifact = self.kernel.kernel.get_artifact(evidence_id)
                if artifact is None:
                    continue
                artifacts_dir.mkdir(exist_ok=True)
                target = artifacts_dir / f"{artifact.id}.txt"
                shutil.copyfile(self.debrief_root.parent / artifact.path, target)
                files.append(
                    self._manifest_entry(target, f"artifacts/{artifact.id}.txt")
                )

        manifest = ReportManifest(
            report_id=debrief.id,
            operation_id=operation_id,
            files=tuple(files),
            finding_ids=tuple(
                finding.id
                for finding in self.kernel.kernel.list_findings(
                    operation_id, status=FindingStatus.VALIDATED
                )
            ),
        )
        (dest / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="debrief.exported",
            payload={"debrief_id": debrief.id, "dest": str(dest), "files": len(files)},
        )
        self.operations.repository.append_event(event)
        return manifest

    def _topological_steps(self, operation_id: str) -> list[AttackStep]:
        steps = self.redteam.list_steps(operation_id)
        ordered: list[AttackStep] = []
        placed: set[str] = set()
        remaining = list(steps)
        while remaining:
            progressed = False
            for step in list(remaining):
                if all(dependency in placed for dependency in step.depends_on):
                    ordered.append(step)
                    placed.add(step.id)
                    remaining.remove(step)
                    progressed = True
            if not progressed:
                ordered.extend(remaining)
                break
        return ordered

    @staticmethod
    def _open_gaps(steps: list[AttackStep]) -> list[str]:
        gaps: list[str] = []
        for step in steps:
            if step.status in (AttackStepStatus.BLOCKED, AttackStepStatus.SKIPPED):
                gaps.append(f"step {step.name} ({step.technique_id}) {step.status.value}")
            elif step.detection in (DetectionStatus.MISSED, DetectionStatus.NOT_OBSERVED):
                gaps.append(
                    f"step {step.name} ({step.technique_id}) detection "
                    f"{step.detection.value}"
                )
        return gaps

    def _build_redactor(self, operation_id: str) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            for metadata in self.vault.list_metadata(operation_id):
                redactor.register(self.vault.get(metadata.name, operation_id))
        return redactor

    def _render_markdown(
        self,
        operation: Operation,
        roe: RoeDocument,
        steps: list[AttackStep],
        checkpoints: list[Checkpoint],
        findings: list[Finding],
        open_gaps: tuple[str, ...],
    ) -> str:
        lines = [
            f"# Red Team Debrief: {operation.name}",
            "",
            f"- operation: {operation.id}",
            "",
            "## Rules of Engagement",
            "",
            "```",
            roe.describe(),
            "```",
            "",
            "## Attack Path",
            "",
        ]
        for index, step in enumerate(steps, start=1):
            lines.append(
                f"{index}. [{step.status.value}] **{step.name}** "
                f"({step.technique_id}) — detection: {step.detection.value}"
            )
            if step.notes:
                lines.append(f"   - notes: {step.notes}")
            if step.expected_telemetry:
                lines.append(f"   - expected telemetry: {step.expected_telemetry}")
            if step.detection_criteria:
                lines.append(f"   - detection criteria: {step.detection_criteria}")
            for evidence_id in step.evidence_ids:
                artifact = self.kernel.kernel.get_artifact(evidence_id)
                if artifact is not None:
                    lines.append(
                        f"   - evidence: {artifact.id} sha256={artifact.sha256}"
                    )
        lines.extend(["", "## Checkpoints", ""])
        if checkpoints:
            for checkpoint in checkpoints:
                lines.append(
                    f"- [{checkpoint.status.value}] {checkpoint.name}"
                    + (f" — {checkpoint.notes}" if checkpoint.notes else "")
                )
        else:
            lines.append("(no checkpoints)")
        lines.extend(["", "## Validated Findings", ""])
        if findings:
            for finding in findings:
                lines.append(f"- {finding.title} ({finding.id})")
        else:
            lines.append("(none)")
        lines.extend(["", "## Open Gaps", ""])
        if open_gaps:
            for gap in open_gaps:
                lines.append(f"- {gap}")
        else:
            lines.append("(none)")
        lines.append("")
        return "\n".join(lines)

    def _render_json(
        self,
        operation: Operation,
        roe: RoeDocument,
        steps: list[AttackStep],
        checkpoints: list[Checkpoint],
        findings: list[Finding],
        open_gaps: tuple[str, ...],
    ) -> str:
        payload = {
            "operation": {
                "id": operation.id,
                "name": operation.name,
                "mode": operation.mode.value,
            },
            "roe": roe.model_dump(mode="json"),
            "attack_path": [step.model_dump(mode="json") for step in steps],
            "checkpoints": [item.model_dump(mode="json") for item in checkpoints],
            "validated_findings": [
                {"id": finding.id, "title": finding.title} for finding in findings
            ],
            "open_gaps": list(open_gaps),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _manifest_entry(path: Path, name: str) -> ManifestFile:
        data = path.read_bytes()
        return ManifestFile(
            path=name,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
