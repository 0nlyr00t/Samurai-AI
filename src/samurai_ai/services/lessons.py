"""Lessons from closed finding verdicts, admitted only with proof.

Extraction is deterministic: a validated or reported finding teaches a positive
lesson, a false positive or duplicate teaches a negative one, and the finding id
travels as evidence so the same outcome cannot be counted twice. Nothing is
injected until the operator switches the mode, and nothing at all until a lesson
is adopted.
"""

from __future__ import annotations

from datetime import UTC, datetime

from samurai_ai.domain.kernel import Finding, FindingStatus
from samurai_ai.domain.lessons import (
    DIGEST_MAX_CHARS,
    DIGEST_MAX_LESSONS,
    Lesson,
    LessonGateError,
    LessonKind,
    LessonMode,
    LessonNotFoundError,
    LessonStatus,
    compose_action,
    compose_cue,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.settings import SettingsService
from samurai_ai.storage.lessons import LessonRepository

MODE_KEY = "lessons.mode"

#: Which verdict teaches what. Anything else is not an outcome yet.
VERDICTS: dict[FindingStatus, LessonKind] = {
    FindingStatus.VALIDATED: LessonKind.POSITIVE,
    FindingStatus.REPORTED: LessonKind.POSITIVE,
    FindingStatus.FALSE_POSITIVE: LessonKind.NEGATIVE,
    FindingStatus.DUPLICATE: LessonKind.NEGATIVE,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LessonService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        lessons: LessonRepository,
        settings: SettingsService,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.lessons = lessons
        self.settings = settings

    # --- mode -------------------------------------------------------------
    def mode(self) -> LessonMode:
        raw = (self.settings.get_value(MODE_KEY) or "").strip()
        try:
            return LessonMode(raw) if raw else LessonMode.SHADOW
        except ValueError:
            return LessonMode.SHADOW

    def set_mode(self, mode: LessonMode) -> None:
        self.settings.set_value(MODE_KEY, mode.value)

    # --- extraction -------------------------------------------------------
    def extract(self, operation_id: str, workspace: str) -> list[Lesson]:
        """Admit lessons for the operation's verdicts; idempotent through evidence."""
        self.operations.get(operation_id)
        admitted: list[Lesson] = []
        for finding in self.kernel.kernel.list_findings(operation_id):
            kind = VERDICTS.get(finding.status)
            if kind is None:
                continue
            lesson = self._admit(operation_id, workspace, finding, kind)
            if lesson is not None:
                admitted.append(lesson)
        return admitted

    def _admit(
        self, operation_id: str, workspace: str, finding: Finding, kind: LessonKind
    ) -> Lesson | None:
        cue = compose_cue(workspace, finding.title)
        action = compose_action(kind, finding.title, finding.summary)
        if not cue or not action or not finding.title.strip():
            raise LessonGateError(f"finding {finding.id} yields neither cue nor action")
        now = _now()
        lesson = self.lessons.upsert(workspace, kind, cue, action, now)
        if not self.lessons.add_evidence(lesson.id, "finding", finding.id, now):
            return None  # this outcome already counted
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type="lesson.admitted",
                payload={"lesson_id": lesson.id, "kind": kind.value, "finding_id": finding.id},
            )
        )
        return lesson

    # --- operator ---------------------------------------------------------
    def get(self, lesson_id: str) -> Lesson:
        lesson = self.lessons.get(lesson_id)
        if lesson is None:
            raise LessonNotFoundError(f"lesson not found: {lesson_id}")
        return lesson

    def all(self, workspace: str | None = None) -> list[Lesson]:
        return self.lessons.list(workspace)

    def support(self, lesson_id: str) -> int:
        return self.lessons.support(lesson_id)

    def adopt(self, lesson_id: str) -> Lesson:
        return self._set_status(lesson_id, LessonStatus.ADOPTED)

    def reject(self, lesson_id: str) -> Lesson:
        return self._set_status(lesson_id, LessonStatus.REJECTED)

    def _set_status(self, lesson_id: str, status: LessonStatus) -> Lesson:
        lesson = self.lessons.set_status(lesson_id, status, _now())
        if lesson is None:
            raise LessonNotFoundError(f"lesson not found: {lesson_id}")
        if lesson.status is LessonStatus.ADOPTED and self.lessons.support(lesson.id) == 0:
            raise LessonGateError(f"lesson {lesson.id} has no evidence and cannot be adopted")
        return lesson

    # --- prompt -----------------------------------------------------------
    def digest(self, workspace: str) -> tuple[str, tuple[str, ...]]:
        """Adopted lessons only, bounded; empty when the mode is not `inject`."""
        if self.mode() is not LessonMode.INJECT:
            return "", ()
        lines: list[str] = []
        used: list[str] = []
        spent = 0
        for lesson in self.lessons.list(workspace):
            if lesson.status is not LessonStatus.ADOPTED:
                continue
            line = f"- [{lesson.kind.value}] {lesson.action}"
            if spent + len(line) > DIGEST_MAX_CHARS:
                break
            lines.append(line)
            used.append(lesson.id)
            spent += len(line)
            if len(lines) >= DIGEST_MAX_LESSONS:
                break
        return "\n".join(lines), tuple(used)

    def shadow_candidates(self, workspace: str) -> list[str]:
        """What `inject` would show: candidate lessons with evidence, newest first."""
        return [
            lesson.id
            for lesson in self.lessons.list(workspace)
            if lesson.status is LessonStatus.CANDIDATE and self.lessons.support(lesson.id) > 0
        ][:DIGEST_MAX_LESSONS]
