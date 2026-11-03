"""Operator questions asked from inside a run.

The wait is bounded: when nobody answers, the run continues and the question
stays pending, so a late answer is still useful instead of a lost prompt.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from samurai_ai.domain.agents import AskOperatorCommand
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.questions import QuestionRepository, QuestionRow

POLL_SECONDS = 0.1
_NOTIFY = Callable[[str], None]


class OperatorQuestionError(ValueError):
    pass


@dataclass(frozen=True)
class AskOutcome:
    """What the ask produced: the model-facing text and whether it timed out."""

    text: str
    call_id: str
    pending: bool


@dataclass(frozen=True)
class Answer:
    id: str
    selected: tuple[str, ...] = ()
    custom: str = ""


class OperatorQuestionService:
    def __init__(
        self, operations: OperationService, questions: QuestionRepository
    ) -> None:
        self.operations = operations
        self.questions = questions
        self._waiters: dict[str, threading.Event] = {}

    def ask(
        self,
        operation_id: str,
        run_id: str,
        command: AskOperatorCommand,
        *,
        notify: _NOTIFY | None = None,
        stop_event: threading.Event | None = None,
    ) -> AskOutcome:
        """Persist the questions, wait at most `timeout`, and report what happened."""
        self._validate(command)
        call_id = f"qcall_{uuid4().hex[:12]}"
        waiter = threading.Event()
        self._waiters[call_id] = waiter
        payload = command.model_dump_json()
        try:
            for question in command.questions:
                self.questions.add(
                    f"{call_id}.{question.id}", operation_id, run_id, call_id, payload
                )
            self._event(
                operation_id,
                "agent.question.asked",
                {"run_id": run_id, "call_id": call_id, "questions": len(command.questions)},
            )
            if notify is not None:
                notify(call_id)
            answered = self._wait(call_id, command.timeout, waiter, stop_event)
        finally:
            self._waiters.pop(call_id, None)
        rows = self.questions.by_call(call_id)
        if answered and all(row.status == "answered" for row in rows):
            answers = [
                {
                    "id": row.id.split(".", 1)[1],
                    "selected": json.loads(row.answer).get("selected", []),
                    "custom": json.loads(row.answer).get("custom", ""),
                }
                for row in rows
            ]
            return AskOutcome(
                json.dumps({"answers": answers}, ensure_ascii=False), call_id, False
            )
        for row in rows:
            if row.status == "pending":
                self.questions.set_status(row.id, "expired")
        return AskOutcome(
            json.dumps(
                {
                    "pending": True,
                    "call_id": call_id,
                    "hint": "nobody answered in time; keep going and check "
                    f"`samurai agent answer {call_id}` later",
                },
                ensure_ascii=False,
            ),
            call_id,
            True,
        )

    def answer(self, call_id: str, answers: list[Answer], by: str) -> list[QuestionRow]:
        rows = self.questions.by_call(call_id)
        if not rows:
            raise OperatorQuestionError(f"unknown question call {call_id}")
        if all(row.status == "answered" for row in rows):
            raise OperatorQuestionError(f"question call {call_id} is already answered")
        by_id = {row.id.split(".", 1)[1]: row for row in rows}
        for answer in answers:
            row = by_id.get(answer.id)
            if row is None:
                raise OperatorQuestionError(f"unknown question id {answer.id!r}")
            payload = json.loads(row.payload)
            question = next(
                item for item in payload["questions"] if item["id"] == answer.id
            )
            labels = {option["label"] for option in question["options"]}
            unknown = [label for label in answer.selected if label not in labels]
            if unknown:
                raise OperatorQuestionError(
                    f"{answer.id}: no such option {unknown[0]!r}"
                )
            self.questions.set_status(
                row.id,
                "answered",
                json.dumps(
                    {"selected": list(answer.selected), "custom": answer.custom},
                    ensure_ascii=False,
                ),
                answered_by=by,
            )
        self._event(
            rows[0].operation_id,
            "agent.question.answered",
            {"call_id": call_id, "by": by},
        )
        waiter = self._waiters.get(call_id)
        if waiter is not None:
            waiter.set()
        return self.questions.by_call(call_id)

    def pending(self, operation_id: str) -> list[QuestionRow]:
        return self.questions.pending(operation_id)

    def _wait(
        self,
        call_id: str,
        timeout: int,
        waiter: threading.Event,
        stop_event: threading.Event | None,
    ) -> bool:
        """Wait for the answer; the store is polled because another process may
        be the one answering, and its in-memory event is not ours."""
        deadline = None if timeout < 0 else time.monotonic() + timeout
        while True:
            if waiter.is_set() or self._answered(call_id):
                return True
            if stop_event is not None and stop_event.is_set():
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False
            waiter.wait(POLL_SECONDS)

    def _answered(self, call_id: str) -> bool:
        rows = self.questions.by_call(call_id)
        return bool(rows) and all(row.status == "answered" for row in rows)

    def _validate(self, command: AskOperatorCommand) -> None:
        seen: set[str] = set()
        for question in command.questions:
            if question.id in seen:
                raise OperatorQuestionError(f"duplicate question id {question.id!r}")
            seen.add(question.id)
            if len({option.label for option in question.options}) != len(
                question.options
            ):
                raise OperatorQuestionError(f"{question.id}: duplicate option label")

    def _event(self, operation_id: str, event_type: str, payload: dict[str, object]) -> None:
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=event_type,
                payload=payload,
            )
        )


__all__ = ["Answer", "AskOutcome", "OperatorQuestionError", "OperatorQuestionService"]
