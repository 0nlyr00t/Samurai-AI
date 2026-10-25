from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from samurai_ai.domain.ctf import (
    ChallengeCategory,
    ChallengeExistsError,
    ChallengeNotFoundError,
    CtfChallenge,
    CtfError,
    CtfModeError,
    CtfWriteup,
    DuplicateFlagError,
    FlagFormatError,
    FlagRecord,
    Hypothesis,
    HypothesisNotFoundError,
    HypothesisStatus,
    WriteupNotFoundError,
    WriteupStateError,
    ctf_template,
)
from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.kernel import ArtifactKind, OperationPlan
from samurai_ai.domain.operations import Operation, OperationEvent, OperationMode, OperationStatus
from samurai_ai.domain.reports import (
    ManifestFile,
    ReportFormat,
    ReportManifest,
)
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.ctf import CtfRepository

_ALLOWED_WRITEUP_STATUSES = (OperationStatus.VERIFICATION, OperationStatus.REPORTING)

HypothesisAction = Literal["testing", "confirm", "refute", "abandon"]


class CtfSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    hypotheses_open: int
    hypotheses_testing: int
    hypotheses_confirmed: int
    hypotheses_refuted: int
    hypotheses_abandoned: int
    flags_captured: int
    writeups: int


class CtfService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        ctf: CtfRepository,
        vault: VaultService | None = None,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.ctf = ctf
        self.vault = vault

    @property
    def writeup_root(self) -> Path:
        return self.ctf.database.path.parent / "reports"

    def init_ctf(
        self,
        operation_id: str,
        name: str,
        category: ChallengeCategory,
        description: str = "",
        win_condition: str = "",
        flag_format: str = "",
    ) -> OperationPlan:
        operation = self.operations.get(operation_id)
        if operation.mode is not OperationMode.CTF_HTB:
            raise CtfModeError(
                f"operation {operation_id} is in {operation.mode.value} mode, not ctf-htb"
            )
        if self.ctf.get_challenge(operation_id) is not None:
            raise ChallengeExistsError(
                f"operation {operation_id} already has a challenge; "
                "playbook init is one-shot"
            )
        challenge = CtfChallenge(
            operation_id=operation_id,
            name=name,
            category=category,
            description=description,
            win_condition=win_condition,
            flag_format=flag_format,
        )
        challenge_event = OperationEvent(
            operation_id=operation_id,
            event_type="challenge.set",
            payload={
                "playbook": "ctf-htb",
                "name": challenge.name,
                "category": challenge.category.value,
            },
        )
        self.ctf.save_challenge(challenge, None, challenge_event)
        template = ctf_template()
        plan = self.kernel.create_plan(
            operation_id, template.title, template.tasks, template.gates
        )
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.NOTE,
            challenge.describe(),
            summary="CTF/HTB challenge brief",
            requirement="challenge-brief",
            gate_id="challenge-locked",
            created_by="operator",
        )
        return plan

    def get_challenge(self, operation_id: str) -> CtfChallenge:
        self.operations.get(operation_id)
        challenge = self.ctf.get_challenge(operation_id)
        if challenge is None:
            raise ChallengeNotFoundError(
                f"operation {operation_id} has no challenge; run challenge init first"
            )
        return challenge

    def propose_hypothesis(
        self,
        operation_id: str,
        title: str,
        category: ChallengeCategory | None = None,
        notes: str = "",
    ) -> Hypothesis:
        self.operations.get(operation_id)
        hypothesis = Hypothesis(
            operation_id=operation_id, title=title, category=category, notes=notes
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="hypothesis.proposed",
            payload={"hypothesis_id": hypothesis.id, "title": title},
        )
        self.ctf.add_hypothesis(hypothesis, event)
        return hypothesis

    def update_hypothesis(
        self,
        hypothesis_id: str,
        action: HypothesisAction,
        notes: str = "",
    ) -> Hypothesis:
        hypothesis = self.ctf.get_hypothesis(hypothesis_id)
        if hypothesis is None:
            raise HypothesisNotFoundError(f"hypothesis {hypothesis_id} not found")
        self.operations.get(hypothesis.operation_id)
        expected_version = hypothesis.version
        if action == "testing":
            hypothesis.begin_testing()
        elif action == "confirm":
            hypothesis.confirm(notes)
        elif action == "refute":
            hypothesis.refute(notes)
        else:
            hypothesis.abandon(notes)
        event = OperationEvent(
            operation_id=hypothesis.operation_id,
            event_type=f"hypothesis.{_action_event_suffix(action)}",
            payload={"hypothesis_id": hypothesis.id, "notes": notes},
        )
        self.ctf.update_hypothesis(hypothesis, expected_version, event)
        return hypothesis

    def list_hypotheses(
        self, operation_id: str, status: HypothesisStatus | None = None
    ) -> list[Hypothesis]:
        self.operations.get(operation_id)
        hypotheses = self.ctf.list_hypotheses(operation_id)
        if status is None:
            return hypotheses
        return [item for item in hypotheses if item.status is status]

    def record_flag(
        self,
        operation_id: str,
        name: str,
        value: str,
        proof_artifact_id: str | None = None,
        submitted_by: str = "operator",
    ) -> FlagRecord:
        self.operations.get(operation_id)
        challenge = self.get_challenge(operation_id)
        if not challenge.accepts_flag(value):
            event = OperationEvent(
                operation_id=operation_id,
                event_type="flag.rejected",
                payload={"name": name, "reason": "format-mismatch"},
            )
            self.operations.repository.append_event(event)
            raise FlagFormatError(
                f"flag {name} does not match the challenge flag format "
                f"{challenge.flag_format!r}"
            )
        if proof_artifact_id is not None:
            artifact = self.kernel.kernel.get_artifact(proof_artifact_id)
            if artifact is None or artifact.operation_id != operation_id:
                raise CtfError(
                    f"proof artifact {proof_artifact_id} not found "
                    f"in operation {operation_id}"
                )
        if self.ctf.get_flag(operation_id, name) is not None:
            event = OperationEvent(
                operation_id=operation_id,
                event_type="flag.duplicate",
                payload={"name": name},
            )
            self.operations.repository.append_event(event)
            raise DuplicateFlagError(f"flag {name} is already recorded")
        record = FlagRecord(
            operation_id=operation_id,
            name=name,
            value=value,
            proof_artifact_id=proof_artifact_id,
            submitted_by=submitted_by,
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="flag.captured",
            payload={"flag_id": record.id, "name": name},
        )
        self.ctf.add_flag(record, event)
        proof_summary = f"Flag {name} captured"
        if proof_artifact_id is not None:
            proof_summary += f" (proof {proof_artifact_id})"
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.NOTE,
            f"{record.name}: {record.value}",
            summary=proof_summary,
            requirement="flag-proof",
            task_id="flag-capture",
            gate_id="flag-captured",
            created_by=submitted_by,
        )
        return record

    def list_flags(self, operation_id: str) -> list[FlagRecord]:
        self.operations.get(operation_id)
        return self.ctf.list_flags(operation_id)

    def summary(self, operation_id: str) -> CtfSummary:
        self.operations.get(operation_id)
        hypotheses = self.ctf.list_hypotheses(operation_id)

        def count(status: HypothesisStatus) -> int:
            return sum(1 for item in hypotheses if item.status is status)

        return CtfSummary(
            operation_id=operation_id,
            hypotheses_open=count(HypothesisStatus.OPEN),
            hypotheses_testing=count(HypothesisStatus.TESTING),
            hypotheses_confirmed=count(HypothesisStatus.CONFIRMED),
            hypotheses_refuted=count(HypothesisStatus.REFUTED),
            hypotheses_abandoned=count(HypothesisStatus.ABANDONED),
            flags_captured=len(self.ctf.list_flags(operation_id)),
            writeups=len(self.ctf.list_writeups(operation_id)),
        )

    def replay(self, operation_id: str) -> str:
        operation = self.operations.get(operation_id)
        lines = [
            f"# Solution Replay: {operation.name}",
            "",
            f"- operation: {operation.id}",
        ]
        challenge = self.ctf.get_challenge(operation_id)
        if challenge is not None:
            lines.append(f"- challenge: {challenge.name} ({challenge.category.value})")
        lines.extend(["", "## Timeline", ""])
        events = [
            event
            for event in self.operations.repository.events(operation_id)
            if event.event_type.startswith(("hypothesis.", "flag.", "challenge."))
        ]
        for event in sorted(events, key=lambda item: item.created_at):
            detail = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"- {event.created_at.isoformat()} `{event.event_type}` {detail}")
        artifacts = self.kernel.kernel.list_artifacts(operation_id)
        lines.extend(["", "## Artifacts", ""])
        if artifacts:
            for artifact in artifacts:
                lines.append(
                    f"- {artifact.id} [{artifact.kind.value}] {artifact.summary} "
                    f"sha256={artifact.sha256}"
                )
        else:
            lines.append("(no artifacts)")
        lines.append("")
        return "\n".join(lines)

    def generate_writeup(
        self,
        operation_id: str,
        format: ReportFormat = ReportFormat.MARKDOWN,
        created_by: str = "operator",
    ) -> CtfWriteup:
        operation = self.operations.get(operation_id)
        if operation.status not in _ALLOWED_WRITEUP_STATUSES:
            raise WriteupStateError(
                f"operation {operation_id} is in {operation.status.value}; "
                "writeups can be generated in verification or reporting"
            )
        challenge = self.get_challenge(operation_id)
        hypotheses = self.ctf.list_hypotheses(operation_id)
        flags = self.ctf.list_flags(operation_id)
        if not flags:
            raise WriteupStateError(
                f"operation {operation_id} has no captured flags to write up"
            )
        replay = self.replay(operation_id)
        if format is ReportFormat.JSON:
            content = self._render_json(operation, challenge, hypotheses, flags, replay)
        else:
            content = self._render_markdown(operation, challenge, hypotheses, flags, replay)
        redacted = self._build_redactor(operation_id).redact(content).text

        data = redacted.encode("utf-8")
        suffix = "json" if format is ReportFormat.JSON else "md"
        writeup_id = f"wr_{uuid4().hex[:12]}"
        writeup = CtfWriteup(
            id=writeup_id,
            operation_id=operation_id,
            format=format,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            path=f"reports/{operation_id}/{writeup_id}.{suffix}",
            flag_ids=tuple(flag.id for flag in flags),
            hypothesis_count=len(hypotheses),
            created_by=created_by,
        )
        directory = self.writeup_root / operation_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{writeup.id}.{suffix}").write_bytes(data)
        event = OperationEvent(
            operation_id=operation_id,
            event_type="writeup.generated",
            payload={
                "writeup_id": writeup.id,
                "format": format.value,
                "flags": len(flags),
            },
        )
        self.ctf.add_writeup(writeup, event)
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.FILE,
            redacted,
            summary=f"CTF/HTB writeup {writeup.id}",
            requirement="final-writeup",
            task_id="writeup",
            gate_id="writeup-ready",
            created_by=created_by,
        )
        return writeup

    def list_writeups(self, operation_id: str) -> list[CtfWriteup]:
        self.operations.get(operation_id)
        return self.ctf.list_writeups(operation_id)

    def read_writeup(self, operation_id: str, writeup_id: str) -> str:
        self.operations.get(operation_id)
        writeup = self.ctf.get_writeup(writeup_id)
        if writeup is None or writeup.operation_id != operation_id:
            raise WriteupNotFoundError(
                f"writeup {writeup_id} not found in operation {operation_id}"
            )
        path = self.writeup_root.parent / writeup.path
        return path.read_text(encoding="utf-8")

    def export_writeup(
        self, operation_id: str, writeup_id: str, dest_dir: Path
    ) -> ReportManifest:
        self.operations.get(operation_id)
        writeup = self.ctf.get_writeup(writeup_id)
        if writeup is None or writeup.operation_id != operation_id:
            raise WriteupNotFoundError(
                f"writeup {writeup_id} not found in operation {operation_id}"
            )
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        files: list[ManifestFile] = []

        suffix = "json" if writeup.format is ReportFormat.JSON else "md"
        writeup_target = dest / f"writeup.{suffix}"
        shutil.copyfile(self.writeup_root.parent / writeup.path, writeup_target)
        files.append(self._manifest_entry(writeup_target, writeup_target.name))

        artifacts_dir = dest / "artifacts"
        flags = self.ctf.list_flags(operation_id)
        for flag in flags:
            if flag.proof_artifact_id is None:
                continue
            artifact = self.kernel.kernel.get_artifact(flag.proof_artifact_id)
            if artifact is None:
                continue
            artifacts_dir.mkdir(exist_ok=True)
            target = artifacts_dir / f"{artifact.id}.txt"
            shutil.copyfile(self.writeup_root.parent / artifact.path, target)
            files.append(self._manifest_entry(target, f"artifacts/{artifact.id}.txt"))

        manifest = ReportManifest(
            report_id=writeup.id,
            operation_id=operation_id,
            files=tuple(files),
            finding_ids=writeup.flag_ids,
        )
        (dest / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="writeup.exported",
            payload={"writeup_id": writeup.id, "dest": str(dest), "files": len(files)},
        )
        self.operations.repository.append_event(event)
        return manifest

    def _build_redactor(self, operation_id: str) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            for metadata in self.vault.list_metadata(operation_id):
                redactor.register(self.vault.get(metadata.name, operation_id))
        return redactor

    def _render_markdown(
        self,
        operation: Operation,
        challenge: CtfChallenge,
        hypotheses: list[Hypothesis],
        flags: list[FlagRecord],
        replay: str,
    ) -> str:
        lines = [
            f"# CTF/HTB Writeup: {challenge.name}",
            "",
            f"- operation: {operation.id}",
            f"- category: {challenge.category.value}",
            f"- win condition: {challenge.win_condition or '(not set)'}",
            "",
            "## Challenge",
            "",
            challenge.description or "(no description)",
            "",
            "## Hypothesis Log",
            "",
        ]
        if hypotheses:
            for hypothesis in hypotheses:
                lines.append(
                    f"- [{hypothesis.status.value}] {hypothesis.title} "
                    f"({hypothesis.id})"
                )
                if hypothesis.notes:
                    lines.append(f"  - notes: {hypothesis.notes}")
        else:
            lines.append("(no hypotheses recorded)")
        lines.extend(["", "## Flag Ledger", ""])
        for flag in flags:
            proof = f" proof={flag.proof_artifact_id}" if flag.proof_artifact_id else ""
            lines.append(f"- {flag.name}: `{flag.value}` ({flag.id}){proof}")
        lines.extend(["", replay, "## Retrospective", "", "(fill in lessons learned)", ""])
        return "\n".join(lines)

    def _render_json(
        self,
        operation: Operation,
        challenge: CtfChallenge,
        hypotheses: list[Hypothesis],
        flags: list[FlagRecord],
        replay: str,
    ) -> str:
        payload = {
            "operation": {
                "id": operation.id,
                "name": operation.name,
                "mode": operation.mode.value,
            },
            "challenge": challenge.model_dump(mode="json"),
            "hypotheses": [item.model_dump(mode="json") for item in hypotheses],
            "flags": [item.model_dump(mode="json") for item in flags],
            "replay": replay,
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


def _action_event_suffix(action: HypothesisAction) -> str:
    return {
        "testing": "testing",
        "confirm": "confirmed",
        "refute": "refuted",
        "abandon": "abandoned",
    }[action]
