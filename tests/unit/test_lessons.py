"""Lessons: admitted only with a cue, an action and evidence; never twice."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity, PlanTask
from samurai_ai.domain.lessons import LessonGateError, LessonKind, LessonMode, LessonStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.lessons import LessonService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.settings import SettingsService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.lessons import LessonRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository

WORKSPACE = "gate-lab"


@pytest.fixture
def stack(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "lessons.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    settings = SettingsService(RecoveryRepository(database))
    lessons = LessonService(operations, kernel, LessonRepository(database), settings)
    operation = operations.create("gate", OperationMode.BUG_BOUNTY, ScopeContract(includes=("x",)))
    kernel.create_plan(operation.id, "plan", [PlanTask(id="recon", title="Recon")], [])
    return lessons, kernel, operations, operation.id


def _validated(kernel: KernelService, operation_id: str, title: str = "IDOR on /user") -> str:
    artifact = kernel.register_artifact(operation_id, ArtifactKind.COMMAND_OUTPUT, "body")
    finding = kernel.create_finding(
        operation_id, title, FindingSeverity.MEDIUM, "cross-tenant read"
    )
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.validate_finding(finding.id)
    return finding.id


def test_a_validated_finding_teaches_a_positive_lesson(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    _validated(kernel, operation_id)

    admitted = lessons.extract(operation_id, WORKSPACE)

    assert len(admitted) == 1
    assert admitted[0].kind is LessonKind.POSITIVE
    assert admitted[0].cue.startswith(f"{WORKSPACE}: ")
    assert "IDOR on /user" in admitted[0].action


def test_a_false_positive_teaches_a_negative_lesson(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    artifact = kernel.register_artifact(operation_id, ArtifactKind.COMMAND_OUTPUT, "body")
    finding = kernel.create_finding(operation_id, "stale JS bundle", FindingSeverity.LOW)
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.mark_false_positive(finding.id)

    admitted = lessons.extract(operation_id, WORKSPACE)

    assert [lesson.kind for lesson in admitted] == [LessonKind.NEGATIVE]
    assert admitted[0].action.startswith("Do not re-report")


def test_a_candidate_finding_teaches_nothing(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    kernel.create_finding(operation_id, "unverified", FindingSeverity.INFO)

    assert lessons.extract(operation_id, WORKSPACE) == []


def test_extraction_is_idempotent(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    _validated(kernel, operation_id)

    lessons.extract(operation_id, WORKSPACE)
    assert lessons.extract(operation_id, WORKSPACE) == []  # nothing new admitted

    stored = lessons.all(WORKSPACE)
    assert len(stored) == 1
    assert lessons.support(stored[0].id) == 1


def test_two_findings_lift_support_of_one_lesson(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    _validated(kernel, operation_id, "IDOR on /user")

    assert lessons.extract(operation_id, WORKSPACE)

    stored = lessons.all(WORKSPACE)[0]
    assert lessons.support(stored.id) == 1


def test_shadow_is_the_default_and_keeps_the_prompt_clean(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    _validated(kernel, operation_id)
    lessons.extract(operation_id, WORKSPACE)

    assert lessons.mode() is LessonMode.SHADOW
    assert lessons.digest(WORKSPACE) == ("", ())
    assert lessons.shadow_candidates(WORKSPACE)


def test_inject_shows_only_adopted_lessons(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    _validated(kernel, operation_id)
    lesson = lessons.extract(operation_id, WORKSPACE)[0]
    lessons.set_mode(LessonMode.INJECT)

    assert lessons.digest(WORKSPACE) == ("", ())  # still a candidate

    lessons.adopt(lesson.id)
    digest, ids = lessons.digest(WORKSPACE)

    assert "IDOR on /user" in digest
    assert ids == (lesson.id,)


def test_a_rejected_lesson_stays_hidden(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    _validated(kernel, operation_id)
    lesson = lessons.extract(operation_id, WORKSPACE)[0]
    lessons.set_mode(LessonMode.INJECT)
    lessons.reject(lesson.id)

    assert lessons.digest(WORKSPACE) == ("", ())
    assert lessons.get(lesson.id).status is LessonStatus.REJECTED


def test_off_mode_silences_the_digest(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    _validated(kernel, operation_id)
    lesson = lessons.extract(operation_id, WORKSPACE)[0]
    lessons.adopt(lesson.id)
    lessons.set_mode(LessonMode.OFF)

    assert lessons.digest(WORKSPACE) == ("", ())


def test_evidence_is_recorded_with_its_source(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    finding_id = _validated(kernel, operation_id)
    lesson = lessons.extract(operation_id, WORKSPACE)[0]

    assert lessons.lessons.evidence(lesson.id) == [("finding", finding_id)]


def test_the_gate_refuses_an_empty_subject(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    artifact = kernel.register_artifact(operation_id, ArtifactKind.COMMAND_OUTPUT, "body")
    finding = kernel.create_finding(operation_id, "  ", FindingSeverity.INFO)
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.mark_false_positive(finding.id)

    with pytest.raises(LessonGateError):
        lessons.extract(operation_id, "")


def test_another_workspace_keeps_its_own_lessons(stack) -> None:
    lessons, kernel, _operations, operation_id = stack
    _validated(kernel, operation_id)
    lessons.extract(operation_id, WORKSPACE)

    assert lessons.all("other-ws") == []
