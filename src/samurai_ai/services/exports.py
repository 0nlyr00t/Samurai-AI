"""Integration exports: CALDERA adversary packs, VECTR campaigns, HTML dossiers.

Export-only integrations (no live APIs). Formats are documented subsets:
- CALDERA: standard pack layout (``adversaries/*.yml`` profile with
  ``atomic_ordering`` + ``abilities/<tactic>/*.yml`` per executed step);
  abilities carry no executor commands (steps are governed plans, not shell).
- VECTR: ``vectr-campaign@v1`` JSON subset for import tooling.
- Dossier: self-contained read-only HTML view of an operation.

Everything is redacted against the vault, hashed, written to disk and
registered as a governed artifact with an export event.
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.kernel import Artifact, ArtifactKind
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.redteam import AttackStepStatus
from samurai_ai.domain.reports import ReportError
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.planning import PlanningService
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.vault import VaultService

CALDERA_DEFAULT_TACTIC = "execution"


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


class IntegrationExportService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        redteam: RedTeamService,
        planning: PlanningService,
        vault: VaultService | None = None,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.redteam = redteam
        self.planning = planning
        self.vault = vault

    def export_caldera(
        self, operation_id: str, dest_dir: Path, created_by: str = "operator"
    ) -> dict[str, Any]:
        operation = self.operations.get(operation_id)
        steps = [
            step
            for step in self.redteam.list_steps(operation_id)
            if step.status is AttackStepStatus.EXECUTED
        ]
        if not steps:
            raise ReportError(
                f"operation {operation_id} has no executed attack steps to export"
            )
        redactor = self._redactor(operation_id)

        ability_files: list[tuple[Path, str]] = []
        ordering: list[str] = []
        for step in steps:
            ability_id = f"samurai-{step.id}"
            ordering.append(ability_id)
            tactic = step.tactic or CALDERA_DEFAULT_TACTIC
            description = step.notes or step.name
            if step.tactic is None:
                description += " [tactic defaulted: execution]"
            ability_yaml = "\n".join(
                [
                    f"id: {_yaml_quote(ability_id)}",
                    f"name: {_yaml_quote(step.name)}",
                    f"description: {_yaml_quote(description)}",
                    f"tactic: {_yaml_quote(tactic)}",
                    f"technique_id: {_yaml_quote(step.technique_id)}",
                    f"technique_name: {_yaml_quote(step.name)}",
                    "executors: []",
                    "",
                ]
            )
            ability_files.append(
                (dest_dir / "abilities" / tactic / f"{ability_id}.yml", ability_yaml)
            )

        profile_yaml = "\n".join(
            [
                f"id: {_yaml_quote(f'samurai-{operation_id}')}",
                f"name: {_yaml_quote(f'Samurai operation {operation.name}')}",
                "description: "
                + _yaml_quote(
                    f"Exported attack path of operation {operation_id} "
                    f"({len(steps)} executed steps)"
                ),
                "atomic_ordering:",
                *(f"- {_yaml_quote(ability_id)}" for ability_id in ordering),
                "",
            ]
        )
        files = [(dest_dir / "adversaries" / f"samurai-{operation_id}.yml", profile_yaml)]
        files.extend(ability_files)

        written: list[dict[str, Any]] = []
        for path, content in files:
            redacted = redactor.redact(content).text
            data = redacted.encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            written.append(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            )

        artifact_content = profile_yaml + "\n# abilities: " + str(len(ability_files))
        self._register_artifact(
            operation_id, artifact_content, f"CALDERA pack ({len(steps)} steps)", created_by
        )
        self._append(
            operation_id,
            "export.generated",
            {
                "format": "caldera-adversary-pack",
                "files": len(written),
                "steps": len(steps),
            },
        )
        return {"format": "caldera-adversary-pack", "files": written}

    def export_vectr(
        self, operation_id: str, dest_file: Path, created_by: str = "operator"
    ) -> dict[str, Any]:
        self.operations.get(operation_id)
        scenario = self.planning.get_scenario(operation_id)
        cases = [
            case
            for case in self.planning.list_test_cases(operation_id)
            if case.status.value != "retired"
        ]
        if not cases:
            raise ReportError(
                f"operation {operation_id} has no active test cases to export"
            )
        redactor = self._redactor(operation_id)
        document = {
            "format": "vectr-campaign@v1",
            "name": scenario.name,
            "description": scenario.description,
            "source_operation": operation_id,
            "test_cases": [
                {
                    "id": case.id,
                    "name": case.name,
                    "technique_id": case.technique_id,
                    "tactic": case.tactic,
                    "kill_chain_phase": case.kill_chain_phase.value,
                    "expected_telemetry": case.expected_telemetry,
                    "detection_expectation": case.detection_expectation,
                    "stop_before": case.stop_before,
                    "status": case.status.value,
                }
                for case in cases
            ],
        }
        text = json.dumps(document, ensure_ascii=False, indent=2)
        redacted = redactor.redact(text).text
        data = redacted.encode("utf-8")
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(data)
        result = {
            "format": "vectr-campaign@v1",
            "files": [
                {
                    "path": str(dest_file),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            ],
        }
        self._register_artifact(
            operation_id, redacted, f"VECTR campaign ({len(cases)} test cases)", created_by
        )
        self._append(
            operation_id,
            "export.generated",
            {"format": "vectr-campaign@v1", "files": 1, "test_cases": len(cases)},
        )
        return result

    def export_dossier(
        self, operation_id: str, dest_file: Path, created_by: str = "operator"
    ) -> dict[str, Any]:
        operation = self.operations.get(operation_id)
        plan = self.kernel.get_current_plan(operation_id)
        findings = self.kernel.kernel.list_findings(operation_id)
        events = self.operations.events(operation_id)
        assets_section = ""
        redactor = self._redactor(operation_id)

        def esc(value: object) -> str:
            return html.escape(str(value))

        lines = [
            "<!DOCTYPE html>",
            '<html lang="en"><head><meta charset="utf-8">',
            f"<title>Samurai dossier {esc(operation.id)}</title>",
            "<style>body{font-family:system-ui,monospace;margin:2em;}"
            "table{border-collapse:collapse;}td,th{border:1px solid #999;"
            "padding:4px 8px;text-align:left;}h1,h2{color:#8b0000;}</style>",
            "</head><body>",
            f"<h1>Operation {esc(operation.name)}</h1>",
            "<table>"
            f"<tr><th>id</th><td>{esc(operation.id)}</td></tr>"
            f"<tr><th>mode</th><td>{esc(operation.mode.value)}</td></tr>"
            f"<tr><th>status</th><td>{esc(operation.status.value)}</td></tr>"
            f"<tr><th>scope includes</th><td>"
            f"{esc(', '.join(operation.scope.includes))}</td></tr>"
            "</table>",
        ]
        if plan is not None:
            lines.append(f"<h2>Plan {esc(plan.title)} (v{plan.version})</h2><ul>")
            for task in plan.tasks:
                lines.append(
                    f"<li>{esc(task.id)} — {esc(task.title)} "
                    f"[{esc(task.status.value)}]</li>"
                )
            lines.append("</ul>")
            if plan.gates:
                lines.append("<h3>Gates</h3><ul>")
                for gate in plan.gates:
                    lines.append(
                        f"<li>{esc(gate.id)} — {esc(gate.name)} "
                        f"[{esc(gate.status.value)}]</li>"
                    )
                lines.append("</ul>")
        lines.append(f"<h2>Findings ({len(findings)})</h2><ul>")
        for finding in findings:
            lines.append(
                f"<li>{esc(finding.title)} — {esc(finding.severity.value)} "
                f"[{esc(finding.status.value)}]</li>"
            )
        lines.append("</ul>")
        lines.append(f"<h2>Event timeline ({len(events)})</h2><ul>")
        for event in events:
            lines.append(
                f"<li>{esc(event.created_at.isoformat())} "
                f"{esc(event.event_type)}</li>"
            )
        lines.append("</ul>")
        lines.append(assets_section)
        lines.append("</body></html>")

        content = redactor.redact("\n".join(lines)).text
        data = content.encode("utf-8")
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(data)
        artifact = self._register_artifact(
            operation_id, content, "Read-only HTML dossier", created_by
        )
        self._append(
            operation_id,
            "dossier.generated",
            {"path": str(dest_file), "artifact_id": artifact.id},
        )
        return {
            "format": "html-dossier",
            "files": [
                {
                    "path": str(dest_file),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            ],
        }

    def _register_artifact(
        self, operation_id: str, content: str, summary: str, created_by: str
    ) -> Artifact:
        return self.kernel.register_artifact(
            operation_id,
            ArtifactKind.FILE,
            content,
            summary=summary,
            created_by=created_by,
        )

    def _redactor(self, operation_id: str) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            seen: set[str] = set()
            for metadata in (
                *self.vault.list_metadata(None),
                *self.vault.list_metadata(operation_id),
            ):
                if metadata.name in seen:
                    continue
                seen.add(metadata.name)
                redactor.register(self.vault.get(metadata.name, operation_id))
        return redactor

    def _append(self, operation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.operations.repository.append_event(
            OperationEvent(operation_id=operation_id, event_type=event_type, payload=payload)
        )
