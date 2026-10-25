from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.kernel import ArtifactKind, OperationPlan
from samurai_ai.domain.operations import (
    Operation,
    OperationEvent,
    OperationMode,
    OperationStatus,
)
from samurai_ai.domain.planning import (
    KillChainPhase,
    PlannedTest,
    PlannedTestDependencyError,
    PlannedTestNotFoundError,
    PlannedTestStatus,
    PlanningModeError,
    PlanScenario,
    Runbook,
    RunbookNotFoundError,
    RunbookStateError,
    ScenarioExistsError,
    ScenarioNotFoundError,
    planning_template,
)
from samurai_ai.domain.reports import ManifestFile, ReportFormat, ReportManifest
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.planning import PlanningRepository

_ALLOWED_RUNBOOK_STATUSES = (
    OperationStatus.PLANNING,
    OperationStatus.APPROVAL,
    OperationStatus.VERIFICATION,
    OperationStatus.REPORTING,
)

TestCaseAction = Literal["ready", "retire"]


class PlanningSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    test_cases_draft: int
    test_cases_ready: int
    test_cases_retired: int
    stop_points: int
    runbooks: int


class PlanningService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        planning: PlanningRepository,
        vault: VaultService | None = None,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.planning = planning
        self.vault = vault

    @property
    def runbook_root(self) -> Path:
        return self.planning.database.path.parent / "reports"

    def init_planning(
        self,
        operation_id: str,
        name: str,
        description: str = "",
        schedule: str = "",
    ) -> OperationPlan:
        operation = self.operations.get(operation_id)
        if operation.mode is not OperationMode.OPERATION_PLAN:
            raise PlanningModeError(
                f"operation {operation_id} is in {operation.mode.value} mode, "
                "not operation-plan"
            )
        if self.planning.get_scenario(operation_id) is not None:
            raise ScenarioExistsError(
                f"operation {operation_id} already has a scenario; "
                "playbook init is one-shot"
            )
        scenario = PlanScenario(
            operation_id=operation_id,
            name=name,
            description=description,
            schedule=schedule,
        )
        scenario_event = OperationEvent(
            operation_id=operation_id,
            event_type="scenario.set",
            payload={"playbook": "operation-plan", "name": scenario.name},
        )
        self.planning.save_scenario(scenario, None, scenario_event)
        template = planning_template()
        plan = self.kernel.create_plan(
            operation_id, template.title, template.tasks, template.gates
        )
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.NOTE,
            scenario.describe(),
            summary="Operation planning scenario brief",
            requirement="scenario-brief",
            task_id="mapping",
            gate_id="scenario-locked",
            created_by="operator",
        )
        return plan

    def get_scenario(self, operation_id: str) -> PlanScenario:
        self.operations.get(operation_id)
        scenario = self.planning.get_scenario(operation_id)
        if scenario is None:
            raise ScenarioNotFoundError(
                f"operation {operation_id} has no scenario; run scenario init first"
            )
        return scenario

    def add_test_case(
        self,
        operation_id: str,
        name: str,
        technique_id: str,
        kill_chain_phase: KillChainPhase,
        expected_telemetry: str = "",
        detection_expectation: str = "",
        depends_on: tuple[str, ...] = (),
        stop_before: bool = False,
        notes: str = "",
    ) -> PlannedTest:
        self.operations.get(operation_id)
        existing = {case.id for case in self.planning.list_test_cases(operation_id)}
        unknown = [dependency for dependency in depends_on if dependency not in existing]
        if unknown:
            raise PlannedTestDependencyError(
                f"test case dependencies not found: {', '.join(unknown)}"
            )
        test_case = PlannedTest(
            operation_id=operation_id,
            name=name,
            technique_id=technique_id,
            kill_chain_phase=kill_chain_phase,
            expected_telemetry=expected_telemetry,
            detection_expectation=detection_expectation,
            depends_on=depends_on,
            stop_before=stop_before,
            notes=notes,
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="test-case.added",
            payload={
                "test_case_id": test_case.id,
                "name": name,
                "technique_id": technique_id,
                "kill_chain_phase": kill_chain_phase.value,
                "depends_on": list(test_case.depends_on),
                "stop_before": stop_before,
            },
        )
        self.planning.add_test_case(test_case, event)
        return test_case

    def update_test_case(
        self, test_case_id: str, action: TestCaseAction, notes: str = ""
    ) -> PlannedTest:
        test_case = self.planning.get_test_case(test_case_id)
        if test_case is None:
            raise PlannedTestNotFoundError(f"test case {test_case_id} not found")
        self.operations.get(test_case.operation_id)
        expected_version = test_case.version
        if action == "ready":
            test_case.mark_ready(notes)
        else:
            test_case.retire(notes)
        event = OperationEvent(
            operation_id=test_case.operation_id,
            event_type=f"test-case.{action if action == 'ready' else 'retired'}",
            payload={"test_case_id": test_case.id, "notes": notes},
        )
        self.planning.update_test_case(test_case, expected_version, event)
        return test_case

    def list_test_cases(
        self, operation_id: str, status: PlannedTestStatus | None = None
    ) -> list[PlannedTest]:
        self.operations.get(operation_id)
        cases = self.planning.list_test_cases(operation_id)
        if status is None:
            return cases
        return [case for case in cases if case.status is status]

    def scenario_map(self, operation_id: str) -> str:
        operation = self.operations.get(operation_id)
        cases = self._topological_cases(operation_id)
        lines = [f"# Scenario Map: {operation.name}", ""]
        if not cases:
            lines.append("(no test cases planned)")
        for index, case in enumerate(cases, start=1):
            marker = " [STOP POINT]" if case.stop_before else ""
            lines.append(
                f"{index}. [{case.status.value}] {case.name} "
                f"({case.technique_id}, {case.kill_chain_phase.value}){marker}"
            )
            if case.depends_on:
                lines.append(f"   depends on: {', '.join(case.depends_on)}")
            if case.expected_telemetry:
                lines.append(f"   expected telemetry: {case.expected_telemetry}")
            if case.detection_expectation:
                lines.append(f"   detection expectation: {case.detection_expectation}")
        lines.append("")
        return "\n".join(lines)

    def summary(self, operation_id: str) -> PlanningSummary:
        self.operations.get(operation_id)
        cases = self.planning.list_test_cases(operation_id)

        def count(status: PlannedTestStatus) -> int:
            return sum(1 for case in cases if case.status is status)

        return PlanningSummary(
            operation_id=operation_id,
            test_cases_draft=count(PlannedTestStatus.DRAFT),
            test_cases_ready=count(PlannedTestStatus.READY),
            test_cases_retired=count(PlannedTestStatus.RETIRED),
            stop_points=sum(1 for case in cases if case.stop_before),
            runbooks=len(self.planning.list_runbooks(operation_id)),
        )

    def generate_runbook(
        self,
        operation_id: str,
        format: ReportFormat = ReportFormat.MARKDOWN,
        created_by: str = "operator",
    ) -> Runbook:
        operation = self.operations.get(operation_id)
        if operation.status not in _ALLOWED_RUNBOOK_STATUSES:
            raise RunbookStateError(
                f"operation {operation_id} is in {operation.status.value}; "
                "runbooks can be generated in planning, approval, verification "
                "or reporting"
            )
        scenario = self.get_scenario(operation_id)
        cases = [
            case
            for case in self._topological_cases(operation_id)
            if case.status is PlannedTestStatus.READY
        ]
        if not cases:
            raise RunbookStateError(
                f"operation {operation_id} has no ready test cases for the runbook"
            )
        if format is ReportFormat.JSON:
            content = self._render_json(operation, scenario, cases)
        else:
            content = self._render_markdown(operation, scenario, cases)
        redacted = self._build_redactor(operation_id).redact(content).text

        data = redacted.encode("utf-8")
        suffix = "json" if format is ReportFormat.JSON else "md"
        runbook_id = f"rb_{uuid4().hex[:12]}"
        runbook = Runbook(
            id=runbook_id,
            operation_id=operation_id,
            format=format,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            path=f"reports/{operation_id}/{runbook_id}.{suffix}",
            test_case_ids=tuple(case.id for case in cases),
            stop_points=sum(1 for case in cases if case.stop_before),
            created_by=created_by,
        )
        directory = self.runbook_root / operation_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{runbook.id}.{suffix}").write_bytes(data)
        event = OperationEvent(
            operation_id=operation_id,
            event_type="runbook.generated",
            payload={
                "runbook_id": runbook.id,
                "format": format.value,
                "test_cases": len(cases),
            },
        )
        self.planning.add_runbook(runbook, event)
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.FILE,
            redacted,
            summary=f"Operation runbook {runbook.id}",
            requirement="final-runbook",
            task_id="runbook",
            gate_id="runbook-ready",
            created_by=created_by,
        )
        return runbook

    def list_runbooks(self, operation_id: str) -> list[Runbook]:
        self.operations.get(operation_id)
        return self.planning.list_runbooks(operation_id)

    def read_runbook(self, operation_id: str, runbook_id: str) -> str:
        self.operations.get(operation_id)
        runbook = self.planning.get_runbook(runbook_id)
        if runbook is None or runbook.operation_id != operation_id:
            raise RunbookNotFoundError(
                f"runbook {runbook_id} not found in operation {operation_id}"
            )
        path = self.runbook_root.parent / runbook.path
        return path.read_text(encoding="utf-8")

    def export_runbook(
        self, operation_id: str, runbook_id: str, dest_dir: Path
    ) -> ReportManifest:
        self.operations.get(operation_id)
        runbook = self.planning.get_runbook(runbook_id)
        if runbook is None or runbook.operation_id != operation_id:
            raise RunbookNotFoundError(
                f"runbook {runbook_id} not found in operation {operation_id}"
            )
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        suffix = "json" if runbook.format is ReportFormat.JSON else "md"
        runbook_target = dest / f"runbook.{suffix}"
        shutil.copyfile(self.runbook_root.parent / runbook.path, runbook_target)
        entry = self._manifest_entry(runbook_target, runbook_target.name)
        manifest = ReportManifest(
            report_id=runbook.id,
            operation_id=operation_id,
            files=(entry,),
            finding_ids=(),
        )
        (dest / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="runbook.exported",
            payload={"runbook_id": runbook.id, "dest": str(dest)},
        )
        self.operations.repository.append_event(event)
        return manifest

    def _topological_cases(self, operation_id: str) -> list[PlannedTest]:
        cases = self.planning.list_test_cases(operation_id)
        ordered: list[PlannedTest] = []
        placed: set[str] = set()
        remaining = list(cases)
        while remaining:
            progressed = False
            for case in list(remaining):
                if all(dependency in placed for dependency in case.depends_on):
                    ordered.append(case)
                    placed.add(case.id)
                    remaining.remove(case)
                    progressed = True
            if not progressed:
                ordered.extend(remaining)
                break
        return ordered

    def _build_redactor(self, operation_id: str) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            for metadata in self.vault.list_metadata(operation_id):
                redactor.register(self.vault.get(metadata.name, operation_id))
        return redactor

    def _render_markdown(
        self,
        operation: Operation,
        scenario: PlanScenario,
        cases: list[PlannedTest],
    ) -> str:
        lines = [
            f"# Operation Runbook: {scenario.name}",
            "",
            f"- operation: {operation.id}",
            f"- schedule: {scenario.schedule or '(not set)'}",
            "",
            "## Scenario",
            "",
            scenario.description or "(no description)",
            "",
            "## Test Cases",
            "",
        ]
        for index, case in enumerate(cases, start=1):
            marker = " **[STOP POINT]**" if case.stop_before else ""
            lines.append(
                f"{index}. **{case.name}** ({case.technique_id}, "
                f"{case.kill_chain_phase.value}){marker}"
            )
            if case.depends_on:
                lines.append(f"   - depends on: {', '.join(case.depends_on)}")
            if case.expected_telemetry:
                lines.append(f"   - expected telemetry: {case.expected_telemetry}")
            if case.detection_expectation:
                lines.append(
                    f"   - detection expectation: {case.detection_expectation}"
                )
            if case.notes:
                lines.append(f"   - notes: {case.notes}")
        stop_points = [case for case in cases if case.stop_before]
        lines.extend(["", "## Stop Points", ""])
        if stop_points:
            for case in stop_points:
                lines.append(f"- before: {case.name} ({case.technique_id})")
        else:
            lines.append("(none)")
        lines.append("")
        return "\n".join(lines)

    def _render_json(
        self,
        operation: Operation,
        scenario: PlanScenario,
        cases: list[PlannedTest],
    ) -> str:
        payload = {
            "operation": {
                "id": operation.id,
                "name": operation.name,
                "mode": operation.mode.value,
            },
            "scenario": scenario.model_dump(mode="json"),
            "test_cases": [case.model_dump(mode="json") for case in cases],
            "stop_points": [case.id for case in cases if case.stop_before],
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
