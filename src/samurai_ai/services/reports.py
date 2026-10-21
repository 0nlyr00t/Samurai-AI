from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.kernel import ArtifactKind, Finding, FindingStatus
from samurai_ai.domain.operations import Operation, OperationEvent, OperationStatus
from samurai_ai.domain.playbooks import AssetLedgerEntry, scope_target_for_asset
from samurai_ai.domain.reports import (
    BountyReport,
    ManifestFile,
    ReportFormat,
    ReportManifest,
    ReportNotFoundError,
    ReportScopeViolationError,
    ReportStateError,
)
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.reports import ReportRepository

_ALLOWED_STATUSES = (OperationStatus.VERIFICATION, OperationStatus.REPORTING)


class ReportService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        playbooks: PlaybookService,
        reports: ReportRepository,
        vault: VaultService | None = None,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.playbooks = playbooks
        self.reports = reports
        self.vault = vault

    def generate(
        self,
        operation_id: str,
        format: ReportFormat = ReportFormat.MARKDOWN,
        created_by: str = "operator",
    ) -> BountyReport:
        operation = self.operations.get(operation_id)
        if operation.status not in _ALLOWED_STATUSES:
            raise ReportStateError(
                f"operation {operation_id} is in {operation.status.value}; "
                "reports can be generated in verification or reporting"
            )
        findings = self.kernel.kernel.list_findings(
            operation_id, status=FindingStatus.VALIDATED
        )
        if not findings:
            raise ReportStateError(
                f"operation {operation_id} has no validated findings to report"
            )
        assets = self.playbooks.list_assets(operation_id)
        self._check_report_scope(operation, findings, assets)

        redactor = self._build_redactor(operation_id)
        if format is ReportFormat.JSON:
            content = self._render_json(operation, findings, assets)
        else:
            content = self._render_markdown(operation, findings, assets)
        redacted = redactor.redact(content).text

        data = redacted.encode("utf-8")
        report_id = f"rep_{uuid4().hex[:12]}"
        suffix = "json" if format is ReportFormat.JSON else "md"
        report = BountyReport(
            id=report_id,
            operation_id=operation_id,
            format=format,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            path=f"reports/{operation_id}/{report_id}.{suffix}",
            finding_ids=tuple(finding.id for finding in findings),
            asset_count=len(assets),
            created_by=created_by,
        )
        directory = self.reports.report_root / operation_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{report.id}.{suffix}").write_bytes(data)
        event = OperationEvent(
            operation_id=operation_id,
            event_type="report.generated",
            payload={
                "report_id": report.id,
                "format": format.value,
                "findings": len(findings),
            },
        )
        self.reports.add_report(report, event)
        plan = self.kernel.kernel.get_current_plan(operation_id)
        gate_id = None
        task_id = None
        if plan is not None:
            gate = next(
                (g for g in plan.gates if "final-report" in g.required_artifacts), None
            )
            task = next(
                (t for t in plan.tasks if "final-report" in t.required_artifacts), None
            )
            gate_id = gate.id if gate is not None else None
            task_id = task.id if task is not None else None
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.FILE,
            redacted,
            summary=f"Bug bounty report {report.id}",
            requirement="final-report",
            task_id=task_id,
            gate_id=gate_id,
            created_by=created_by,
        )
        return report

    def export(
        self, operation_id: str, report_id: str, dest_dir: Path
    ) -> ReportManifest:
        self.operations.get(operation_id)
        report = self.reports.get_report(report_id)
        if report is None or report.operation_id != operation_id:
            raise ReportNotFoundError(
                f"report {report_id} not found in operation {operation_id}"
            )
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        files: list[ManifestFile] = []

        suffix = "json" if report.format is ReportFormat.JSON else "md"
        report_target = dest / f"report.{suffix}"
        shutil.copyfile(self.reports.report_root.parent / report.path, report_target)
        files.append(self._manifest_entry(report_target, report_target.name))

        artifacts_dir = dest / "artifacts"
        for finding_id in report.finding_ids:
            finding = self.kernel.kernel.get_finding(finding_id)
            if finding is None:
                continue
            for evidence_id in finding.evidence_ids:
                artifact = self.kernel.kernel.get_artifact(evidence_id)
                if artifact is None:
                    continue
                artifacts_dir.mkdir(exist_ok=True)
                target = artifacts_dir / f"{artifact.id}.txt"
                shutil.copyfile(
                    self.reports.report_root.parent / artifact.path, target
                )
                files.append(self._manifest_entry(target, f"artifacts/{artifact.id}.txt"))

        manifest = ReportManifest(
            report_id=report.id,
            operation_id=operation_id,
            files=tuple(files),
            finding_ids=report.finding_ids,
        )
        (dest / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="report.exported",
            payload={
                "report_id": report.id,
                "dest": str(dest),
                "files": len(files),
            },
        )
        self.operations.repository.append_event(event)
        return manifest

    def mark_reported(self, operation_id: str, finding_ids: list[str]) -> list[Finding]:
        self.operations.get(operation_id)
        updated: list[Finding] = []
        for finding_id in finding_ids:
            finding = self.kernel.mark_reported(finding_id)
            updated.append(finding)
        return updated

    def _check_report_scope(
        self,
        operation: Operation,
        findings: list[Finding],
        assets: list[AssetLedgerEntry],
    ) -> None:
        targets = {
            scope_target_for_asset(entry.kind, entry.value) for entry in assets
        }
        for finding in findings:
            record = self.playbooks.playbooks.get_fingerprint_for_finding(finding.id)
            if record is not None:
                targets.add(record.asset)
        violations = [
            target
            for target in sorted(targets)
            if not operation.scope.evaluate(target).allowed
        ]
        if violations:
            event = OperationEvent(
                operation_id=operation.id,
                event_type="report.rejected",
                payload={"reason": "out-of-scope", "targets": violations},
            )
            self.operations.repository.append_event(event)
            raise ReportScopeViolationError(
                f"report references out-of-scope targets: {', '.join(violations)}"
            )

    def _build_redactor(self, operation_id: str) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            for metadata in self.vault.list_metadata(operation_id):
                redactor.register(self.vault.get(metadata.name, operation_id))
        return redactor

    def _render_markdown(
        self,
        operation: Operation,
        findings: list[Finding],
        assets: list[AssetLedgerEntry],
    ) -> str:
        lines = [
            f"# Bug Bounty Report: {operation.name}",
            "",
            f"- operation: {operation.id}",
            f"- mode: {operation.mode.value}",
            f"- scope includes: {', '.join(operation.scope.includes)}",
            f"- scope excludes: {', '.join(operation.scope.excludes) or '(none)'}",
            "",
        ]
        rules = self.playbooks.playbooks.get_rules(operation.id)
        if rules is not None:
            lines.extend(["## Program Rules", "", "```", rules.describe(), "```", ""])
        lines.append("## Assets")
        lines.append("")
        if assets:
            for entry in assets:
                lines.append(f"- `{entry.kind.value}` {entry.value}")
        else:
            lines.append("(no assets recorded)")
        lines.append("")
        lines.append("## Validated Findings")
        lines.append("")
        for finding in findings:
            lines.extend(
                [
                    f"### {finding.title}",
                    "",
                    f"- id: {finding.id}",
                    f"- severity: {finding.severity.value}",
                    f"- summary: {finding.summary or '(none)'}",
                    f"- remediation: {finding.remediation or '(none)'}",
                    "- evidence:",
                ]
            )
            for evidence_id in finding.evidence_ids:
                artifact = self.kernel.kernel.get_artifact(evidence_id)
                if artifact is None:
                    continue
                lines.append(
                    f"  - {artifact.id} sha256={artifact.sha256} {artifact.summary}"
                )
            lines.append("")
        return "\n".join(lines)

    def _render_json(
        self,
        operation: Operation,
        findings: list[Finding],
        assets: list[AssetLedgerEntry],
    ) -> str:
        payload = {
            "operation": {
                "id": operation.id,
                "name": operation.name,
                "mode": operation.mode.value,
                "scope": operation.scope.model_dump(mode="json"),
            },
            "assets": [
                {"kind": entry.kind.value, "value": entry.value} for entry in assets
            ],
            "findings": [
                {
                    "id": finding.id,
                    "title": finding.title,
                    "severity": finding.severity.value,
                    "summary": finding.summary,
                    "remediation": finding.remediation,
                    "evidence": [
                        artifact.model_dump(mode="json")
                        for evidence_id in finding.evidence_ids
                        if (artifact := self.kernel.kernel.get_artifact(evidence_id))
                        is not None
                    ],
                }
                for finding in findings
            ],
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
