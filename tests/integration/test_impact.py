"""Impact ladder: evidence-backed levels, rollback notes, bounded budget."""

from pathlib import Path

import pytest

from samurai_ai.domain.impact import (
    MAX_ATTEMPTS,
    ImpactBudgetError,
    ImpactError,
    ImpactLevel,
)
from samurai_ai.domain.kernel import ArtifactKind
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.impact import ImpactService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository


def make_stack(tmp_path: Path) -> tuple[KernelService, ImpactService, str]:
    database = Database(tmp_path / "impact.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    operation = operations.create(
        "Impact lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("example.com",))
    )
    artifact = kernel.register_artifact(
        operation.id, ArtifactKind.COMMAND_OUTPUT, "proof", summary="proof"
    )
    finding = kernel.create_finding(
        operation.id, "IDOR on orders", summary="read other orders"
    )
    kernel.attach_evidence(finding.id, artifact.id)
    return kernel, ImpactService(operations, kernel), finding.id


def test_a_level_needs_evidence(tmp_path: Path) -> None:
    kernel, impact, finding_id = make_stack(tmp_path)
    with pytest.raises(ImpactError, match="needs evidence"):
        impact.record(finding_id, ImpactLevel.READ, ())


def test_a_state_change_needs_a_rollback_note(tmp_path: Path) -> None:
    kernel, impact, finding_id = make_stack(tmp_path)
    with pytest.raises(ImpactError, match="rollback note"):
        impact.record(finding_id, ImpactLevel.WRITE, ("trf_1",))


def test_levels_escalate_and_grade(tmp_path: Path) -> None:
    kernel, impact, finding_id = make_stack(tmp_path)
    state = impact.record(finding_id, ImpactLevel.READ, ("trf_1",))
    assert state.points == 1 and not state.reportable
    assert impact.suggest(finding_id) is ImpactLevel.WRITE

    state = impact.record(
        finding_id, ImpactLevel.AUTH_BYPASS, ("trf_2",), rollback="delete the created user"
    )
    assert state.points == 3 and state.reportable
    assert state.level is ImpactLevel.AUTH_BYPASS
    # confirmed levels bump the evidence version, reports must be re-rendered
    assert kernel.get_finding_by_id(finding_id).evidence_version == 2


def test_the_budget_counts_every_recorded_attempt(tmp_path: Path) -> None:
    kernel, impact, finding_id = make_stack(tmp_path)
    state = impact.record(finding_id, ImpactLevel.READ, ("trf_1",))
    assert state.budget.attempts == 1
    state = impact.record(finding_id, ImpactLevel.WRITE, ("trf_2",), rollback="undo")
    assert state.budget.attempts == 2
    assert state.budget.remaining_attempts == state.budget.max_attempts - 2


def test_an_exhausted_budget_refuses_the_next_attempt(tmp_path: Path) -> None:
    kernel, impact, finding_id = make_stack(tmp_path)
    for index in range(MAX_ATTEMPTS):
        impact.record(finding_id, ImpactLevel.READ, (f"trf_{index}",))
    with pytest.raises(ImpactBudgetError, match="spent"):
        impact.record(finding_id, ImpactLevel.RCE, ("trf_x",), rollback="undo")


def test_grade_before_any_level_is_refused(tmp_path: Path) -> None:
    kernel, impact, finding_id = make_stack(tmp_path)
    with pytest.raises(ImpactError, match="no confirmed impact level"):
        impact.grade(finding_id)
