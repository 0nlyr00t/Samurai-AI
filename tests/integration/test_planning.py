import json
from pathlib import Path

import pytest

from samurai_ai.domain.operations import Operation, OperationMode, OperationStatus
from samurai_ai.domain.planning import (
    KillChainPhase,
    PlannedTestDependencyError,
    PlannedTestStateError,
    PlannedTestStatus,
    PlanningModeError,
    RunbookStateError,
    ScenarioExistsError,
    ScenarioNotFoundError,
)
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.planning import PlanningService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.planning import PlanningRepository
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
        self.planning = PlanningService(
            self.operations, self.kernel, PlanningRepository(database), vault
        )


def make_operation(stack: Stack) -> Operation:
    operation = stack.operations.create(
        "Emulation plan",
        OperationMode.OPERATION_PLAN,
        ScopeContract(includes=("lab.local",)),
    )
    stack.planning.init_planning(
        operation.id,
        "Q3 finance emulation",
        description="emulate APT on the finance segment",
        schedule="weekdays 09:00-12:00",
    )
    return operation


def test_init_is_one_shot_and_mode_checked(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    with pytest.raises(ScenarioExistsError):
        stack.planning.init_planning(operation.id, "again")

    rt = stack.operations.create(
        "RT", OperationMode.RED_TEAM, ScopeContract(includes=("corp.local",))
    )
    with pytest.raises(PlanningModeError):
        stack.planning.init_planning(rt.id, "nope")
    with pytest.raises(ScenarioNotFoundError):
        stack.planning.get_scenario(rt.id)


def test_init_creates_plan_and_scenario_brief(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    plan = stack.kernel.kernel.get_current_plan(operation.id)
    assert plan is not None
    assert [task.id for task in plan.tasks] == [
        "scenario-draft",
        "mapping",
        "test-design",
        "review",
        "runbook",
    ]
    assert stack.kernel.kernel.artifact_exists(operation.id, "scenario-brief")
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "scenario.set" in events


def test_add_test_case_validates_dependencies(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    with pytest.raises(PlannedTestDependencyError, match="not found"):
        stack.planning.add_test_case(
            operation.id,
            "dump",
            "T1003",
            KillChainPhase.INSTALLATION,
            depends_on=("tc_missing",),
        )
    first = stack.planning.add_test_case(
        operation.id, "recon", "T1595", KillChainPhase.RECONNAISSANCE
    )
    second = stack.planning.add_test_case(
        operation.id,
        "phish",
        "T1566.001",
        KillChainPhase.DELIVERY,
        depends_on=(first.id,),
        stop_before=True,
    )
    assert second.depends_on == (first.id,)
    assert second.stop_before is True


def test_test_case_transitions(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    case = stack.planning.add_test_case(
        operation.id, "c2", "T1071.001", KillChainPhase.COMMAND_AND_CONTROL
    )
    ready = stack.planning.update_test_case(case.id, "ready", "reviewed")
    assert ready.status is PlannedTestStatus.READY
    with pytest.raises(PlannedTestStateError):
        stack.planning.update_test_case(case.id, "ready")
    retired = stack.planning.update_test_case(case.id, "retire", "replaced")
    assert retired.status is PlannedTestStatus.RETIRED
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "test-case.added" in events
    assert "test-case.ready" in events
    assert "test-case.retired" in events


def test_runbook_guards(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    # DRAFT status is not allowed
    with pytest.raises(RunbookStateError, match="planning, approval"):
        stack.planning.generate_runbook(operation.id)

    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)
    # no ready test cases yet
    stack.planning.add_test_case(
        operation.id, "recon", "T1595", KillChainPhase.RECONNAISSANCE
    )
    with pytest.raises(RunbookStateError, match="no ready test cases"):
        stack.planning.generate_runbook(operation.id)


def test_runbook_generation_in_planning_status(tmp_path: Path) -> None:
    """Planning mode: the runbook is produced without entering execution."""
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    recon = stack.planning.add_test_case(
        operation.id,
        "passive recon",
        "T1595.002",
        KillChainPhase.RECONNAISSANCE,
        expected_telemetry="none (passive)",
        detection_expectation="no alerts expected",
    )
    phish = stack.planning.add_test_case(
        operation.id,
        "spearphish",
        "T1566.001",
        KillChainPhase.DELIVERY,
        depends_on=(recon.id,),
        stop_before=True,
        expected_telemetry="email gateway log",
    )
    stack.planning.update_test_case(recon.id, "ready")
    stack.planning.update_test_case(phish.id, "ready")
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)

    runbook = stack.planning.generate_runbook(operation.id)
    text = (tmp_path / runbook.path).read_text(encoding="utf-8")
    assert "Operation Runbook: Q3 finance emulation" in text
    assert "weekdays 09:00-12:00" in text
    assert "T1595.002" in text
    assert "T1566.001" in text
    assert "[STOP POINT]" in text
    assert text.index("passive recon") < text.index("spearphish")
    assert runbook.stop_points == 1
    assert stack.kernel.kernel.artifact_exists(operation.id, "final-runbook")
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "runbook.generated" in events


def test_runbook_skips_draft_and_retired(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    ready = stack.planning.add_test_case(
        operation.id, "recon", "T1595", KillChainPhase.RECONNAISSANCE
    )
    stack.planning.add_test_case(
        operation.id, "draft case", "T1590", KillChainPhase.RECONNAISSANCE
    )
    stack.planning.update_test_case(ready.id, "ready")
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)

    runbook = stack.planning.generate_runbook(operation.id)
    assert runbook.test_case_ids == (ready.id,)
    text = (tmp_path / runbook.path).read_text(encoding="utf-8")
    assert "draft case" not in text


def test_runbook_json_format(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    case = stack.planning.add_test_case(
        operation.id, "recon", "T1595", KillChainPhase.RECONNAISSANCE
    )
    stack.planning.update_test_case(case.id, "ready")
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)

    runbook = stack.planning.generate_runbook(operation.id, ReportFormat.JSON)
    payload = json.loads((tmp_path / runbook.path).read_text(encoding="utf-8"))
    assert payload["scenario"]["name"] == "Q3 finance emulation"
    assert payload["test_cases"][0]["technique_id"] == "T1595"
    assert stack.planning.read_runbook(operation.id, runbook.id)


def test_runbook_redacts_vault_secrets(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db", with_vault=True)
    assert stack.vault is not None
    operation = make_operation(stack)
    stack.vault.add("lab-key", "plan-secret-abc-123", operation.id)
    case = stack.planning.add_test_case(
        operation.id,
        "recon",
        "T1595",
        KillChainPhase.RECONNAISSANCE,
        notes="account plan-secret-abc-123",
    )
    stack.planning.update_test_case(case.id, "ready")
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)

    runbook = stack.planning.generate_runbook(operation.id)
    text = (tmp_path / runbook.path).read_text(encoding="utf-8")
    assert "plan-secret-abc-123" not in text
    assert "***REDACTED***" in text


def test_export_runbook_manifest(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    case = stack.planning.add_test_case(
        operation.id, "recon", "T1595", KillChainPhase.RECONNAISSANCE
    )
    stack.planning.update_test_case(case.id, "ready")
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)
    runbook = stack.planning.generate_runbook(operation.id)

    dest = tmp_path / "export"
    manifest = stack.planning.export_runbook(operation.id, runbook.id, dest)
    assert (dest / "runbook.md").exists()
    assert (dest / "manifest.json").exists()
    assert [entry.path for entry in manifest.files] == ["runbook.md"]
    payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert payload["report_id"] == runbook.id
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "runbook.exported" in events


def test_scenario_map_and_summary(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    first = stack.planning.add_test_case(
        operation.id, "recon", "T1595", KillChainPhase.RECONNAISSANCE
    )
    stack.planning.add_test_case(
        operation.id,
        "phish",
        "T1566",
        KillChainPhase.DELIVERY,
        depends_on=(first.id,),
        stop_before=True,
    )
    stack.planning.update_test_case(first.id, "ready")

    scenario_map = stack.planning.scenario_map(operation.id)
    assert scenario_map.index("recon") < scenario_map.index("phish")
    assert "[STOP POINT]" in scenario_map

    summary = stack.planning.summary(operation.id)
    assert summary.test_cases_ready == 1
    assert summary.test_cases_draft == 1
    assert summary.stop_points == 1
    assert summary.runbooks == 0
