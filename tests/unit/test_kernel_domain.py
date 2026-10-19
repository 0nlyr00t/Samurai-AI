import pytest
from pydantic import ValidationError

from samurai_ai.domain.kernel import (
    Finding,
    FindingStateError,
    FindingStatus,
    OperationPlan,
    PlanTask,
    StageGate,
    TaskStatus,
)


def make_task(task_id: str, depends_on: tuple[str, ...] = ()) -> PlanTask:
    return PlanTask(id=task_id, title=f"Task {task_id}", depends_on=depends_on)


def test_plan_accepts_valid_structure() -> None:
    plan = OperationPlan(
        operation_id="op_1",
        title="Engagement",
        tasks=(
            make_task("recon"),
            make_task("exploit", depends_on=("recon",)),
            make_task("report", depends_on=("recon", "exploit")),
        ),
        gates=(StageGate(id="go-no-go", name="Go / no-go"),),
    )
    assert [task.id for task in plan.tasks] == ["recon", "exploit", "report"]
    assert plan.gates[0].id == "go-no-go"


def test_task_and_gate_ids_must_be_kebab_case() -> None:
    with pytest.raises(ValidationError, match="kebab-case"):
        make_task("Not_Kebab")
    with pytest.raises(ValidationError, match="kebab-case"):
        StageGate(id="Gate One", name="Gate")


def test_duplicate_task_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="task ids must be unique"):
        OperationPlan(
            operation_id="op_1",
            title="Plan",
            tasks=(make_task("recon"), make_task("recon")),
        )


def test_duplicate_gate_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="gate ids must be unique"):
        OperationPlan(
            operation_id="op_1",
            title="Plan",
            gates=(
                StageGate(id="gate", name="One"),
                StageGate(id="gate", name="Two"),
            ),
        )


def test_unknown_dependencies_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown tasks: ghost"):
        OperationPlan(
            operation_id="op_1",
            title="Plan",
            tasks=(make_task("recon", depends_on=("ghost",)),),
        )


def test_dependency_cycles_are_rejected() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        OperationPlan(
            operation_id="op_1",
            title="Plan",
            tasks=(
                make_task("alpha", depends_on=("beta",)),
                make_task("beta", depends_on=("alpha",)),
            ),
        )


def test_self_dependency_is_rejected_as_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        OperationPlan(
            operation_id="op_1",
            title="Plan",
            tasks=(make_task("alpha", depends_on=("alpha",)),),
        )


def test_finding_follows_the_evidence_lifecycle() -> None:
    finding = Finding(operation_id="op_1", title="Reflected XSS")
    assert finding.status == FindingStatus.CANDIDATE
    assert finding.version == 1

    with pytest.raises(FindingStateError):
        finding.mark_validated()

    finding.begin_validation()
    assert finding.status == FindingStatus.VALIDATING

    with pytest.raises(FindingStateError, match="requires evidence"):
        finding.mark_validated()

    finding.attach_evidence("art_1")
    finding.attach_evidence("art_1")
    assert finding.evidence_ids == ("art_1",)

    finding.mark_validated()
    assert finding.status == FindingStatus.VALIDATED

    finding.mark_reported()
    assert finding.status == FindingStatus.REPORTED
    assert finding.version > 1


def test_finding_cannot_restart_validation() -> None:
    finding = Finding(operation_id="op_1", title="SSRF")
    finding.begin_validation()

    with pytest.raises(FindingStateError):
        finding.begin_validation()


def test_finding_report_requires_validated_status() -> None:
    finding = Finding(operation_id="op_1", title="Open redirect")

    with pytest.raises(FindingStateError):
        finding.mark_reported()


def test_finding_false_positive_from_candidate_blocks_evidence() -> None:
    finding = Finding(operation_id="op_1", title="Weak cipher")
    finding.mark_false_positive()
    assert finding.status == FindingStatus.FALSE_POSITIVE

    with pytest.raises(FindingStateError):
        finding.attach_evidence("art_1")


def test_finding_duplicate_records_original() -> None:
    finding = Finding(operation_id="op_1", title="Duplicate CVE")
    finding.mark_duplicate("find_original")
    assert finding.status == FindingStatus.DUPLICATE
    assert finding.duplicate_of == "find_original"


def test_finding_cannot_duplicate_itself() -> None:
    finding = Finding(operation_id="op_1", title="Self duplicate")

    with pytest.raises(FindingStateError):
        finding.mark_duplicate(finding.id)


def test_task_defaults_are_pending_with_empty_dependencies() -> None:
    task = make_task("recon")
    assert task.status == TaskStatus.PENDING
    assert task.depends_on == ()
    assert task.required_artifacts == ()


def test_empty_requirements_are_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot be empty"):
        PlanTask(id="recon", title="Recon", required_artifacts=("  ",))
