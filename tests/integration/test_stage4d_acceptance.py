"""Stage 4d acceptance criteria as executable tests (Operation Planning).

A planning operation produces a scenario with ATT&CK and kill chain mapping,
test cases with detection expectations, stop points and a redacted runbook
exported with a SHA-256 manifest — without running any active actions.
The whole state survives a database reopen.
"""

import hashlib
import json
from pathlib import Path

import pytest

from samurai_ai.domain.kernel import ArtifactKind
from samurai_ai.domain.operations import OperationMode, OperationStatus
from samurai_ai.domain.planning import (
    KillChainPhase,
    PlannedTestDependencyError,
    PlanningModeError,
    RunbookStateError,
    ScenarioExistsError,
)
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
    def __init__(self, database_path: Path) -> None:
        database = Database(database_path)
        self.operations = OperationService(OperationRepository(database))
        self.kernel = KernelService(self.operations, KernelRepository(database))
        self.vault = VaultService(
            VaultRepository(database),
            self.operations.repository,
            database_path.parent / "vault.key",
        )
        self.planning = PlanningService(
            self.operations, self.kernel, PlanningRepository(database), self.vault
        )


def test_stage4d_operation_planning_acceptance(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    stack = Stack(database_path)
    operation = stack.operations.create(
        "Acceptance plan",
        OperationMode.OPERATION_PLAN,
        ScopeContract(includes=("lab.local",)),
        objective="Plan an APT emulation without active actions",
    )
    operation_id = operation.id

    # --- scenario init: brief, template plan, scenario-brief artifact ---
    plan = stack.planning.init_planning(
        operation_id,
        "Finance segment emulation",
        description="emulate an APT against the finance segment",
        schedule="weekdays 09:00-12:00",
    )
    assert [task.id for task in plan.tasks] == [
        "scenario-draft",
        "mapping",
        "test-design",
        "review",
        "runbook",
    ]
    assert stack.kernel.kernel.artifact_exists(operation_id, "scenario-brief")

    # --- planning lifecycle: no execution status needed for the runbook ---
    stack.kernel.transition_operation(operation_id, OperationStatus.PLANNING)

    stack.kernel.start_task(operation_id, "scenario-draft")
    stack.kernel.complete_task(operation_id, "scenario-draft")
    stack.kernel.start_task(operation_id, "mapping")

    # --- test cases with ATT&CK, kill chain, expectations, deps, stop points ---
    recon = stack.planning.add_test_case(
        operation_id,
        "passive recon",
        "T1595.002",
        KillChainPhase.RECONNAISSANCE,
        expected_telemetry="none (passive)",
        detection_expectation="no alerts expected",
    )
    phish = stack.planning.add_test_case(
        operation_id,
        "spearphish finance",
        "T1566.001",
        KillChainPhase.DELIVERY,
        depends_on=(recon.id,),
        stop_before=True,
        expected_telemetry="email gateway log",
        detection_expectation="gateway detonation alert",
    )
    beacon = stack.planning.add_test_case(
        operation_id,
        "c2 beacon",
        "T1071.001",
        KillChainPhase.COMMAND_AND_CONTROL,
        depends_on=(phish.id,),
        expected_telemetry="periodic HTTPS flows",
        detection_expectation=" beaconing heuristic",
    )
    with pytest.raises(PlannedTestDependencyError):
        stack.planning.add_test_case(
            operation_id,
            "broken",
            "T1003",
            KillChainPhase.INSTALLATION,
            depends_on=("tc_missing",),
        )

    # a secret that must never leak into the runbook
    stack.vault.add("lab-account", "plan-secret-zzz-1", operation_id)
    stealth = stack.planning.add_test_case(
        operation_id,
        "exfil dry run",
        "T1041",
        KillChainPhase.ACTIONS_ON_OBJECTIVES,
        depends_on=(beacon.id,),
        notes="use account plan-secret-zzz-1",
    )

    for case in (recon, phish, beacon, stealth):
        stack.planning.update_test_case(case.id, "ready")

    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.NOTE,
        "ATT&CK and kill chain mapping",
        requirement="attack-mapping",
        task_id="test-design",
        gate_id="mapping-complete",
    )
    stack.kernel.complete_task(operation_id, "mapping")
    stack.kernel.start_task(operation_id, "test-design")
    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.NOTE,
        "test case catalogue",
        requirement="test-cases",
        task_id="review",
        gate_id="tests-ready",
    )
    stack.kernel.complete_task(operation_id, "test-design")
    stack.kernel.start_task(operation_id, "review")
    stack.kernel.complete_task(operation_id, "review")

    # --- runbook generation in PLANNING status: no active actions executed ---
    runbook = stack.planning.generate_runbook(operation_id)
    text = (tmp_path / runbook.path).read_text(encoding="utf-8")
    assert "Finance segment emulation" in text
    assert "weekdays 09:00-12:00" in text
    assert "T1595.002" in text
    assert "reconnaissance" in text
    assert "command-and-control" in text
    assert "**[STOP POINT]**" in text
    assert "before: spearphish finance (T1566.001)" in text
    assert runbook.stop_points == 1
    assert text.index("passive recon") < text.index("spearphish finance")
    # secrets never leak
    assert "plan-secret-zzz-1" not in text
    assert "***REDACTED***" in text

    stack.kernel.start_task(operation_id, "runbook")
    stack.kernel.complete_task(operation_id, "runbook")

    # --- continue the lifecycle to reporting ---
    stack.kernel.transition_operation(operation_id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.EXECUTION)
    stack.kernel.transition_operation(operation_id, OperationStatus.VERIFICATION)
    for gate_id in (
        "scenario-locked",
        "mapping-complete",
        "tests-ready",
        "runbook-ready",
    ):
        stack.kernel.pass_gate(operation_id, gate_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.REPORTING)

    # --- runbook export with manifest ---
    dest = tmp_path / "package"
    manifest = stack.planning.export_runbook(operation_id, runbook.id, dest)
    assert (dest / "runbook.md").exists()
    manifest_payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest_payload["files"]:
        data = (dest / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert manifest.report_id == runbook.id

    stack.kernel.transition_operation(operation_id, OperationStatus.COMPLETE)

    # --- journal carries the governed trail ---
    event_types = [event.event_type for event in stack.operations.events(operation_id)]
    for expected in (
        "scenario.set",
        "test-case.added",
        "test-case.ready",
        "runbook.generated",
        "runbook.exported",
    ):
        assert expected in event_types, expected

    # --- everything survives a reopen ---
    reopened = Stack(database_path)
    restored = reopened.operations.get(operation_id)
    assert restored.status is OperationStatus.COMPLETE
    scenario = reopened.planning.get_scenario(operation_id)
    assert scenario.name == "Finance segment emulation"
    assert len(reopened.planning.list_test_cases(operation_id)) == 4
    restored_runbook = reopened.planning.planning.get_runbook(runbook.id)
    assert restored_runbook is not None
    assert restored_runbook.sha256 == runbook.sha256
    assert "T1071.001" in reopened.planning.scenario_map(operation_id)


def test_stage4d_guards(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = stack.operations.create(
        "Guarded plan",
        OperationMode.OPERATION_PLAN,
        ScopeContract(includes=("lab.local",)),
    )
    stack.planning.init_planning(operation.id, "Guarded scenario")

    # one-shot guard
    with pytest.raises(ScenarioExistsError):
        stack.planning.init_planning(operation.id, "again")

    # runbook is not allowed in DRAFT status
    with pytest.raises(RunbookStateError, match="planning, approval"):
        stack.planning.generate_runbook(operation.id)

    # runbook requires at least one ready test case
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)
    with pytest.raises(RunbookStateError, match="no ready test cases"):
        stack.planning.generate_runbook(operation.id)

    # planning init is rejected on a red-team operation
    rt = stack.operations.create(
        "Wrong mode",
        OperationMode.RED_TEAM,
        ScopeContract(includes=("corp.local",)),
    )
    with pytest.raises(PlanningModeError):
        stack.planning.init_planning(rt.id, "nope")
