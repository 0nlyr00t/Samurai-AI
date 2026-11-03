"""Asking the operator mid-run: answered in time, or left open without blocking."""

import json
from pathlib import Path

import pytest

from samurai_ai.domain.agents import AskOperatorCommand
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationService
from samurai_ai.services.questions import (
    Answer,
    OperatorQuestionError,
    OperatorQuestionService,
)
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.questions import QuestionRepository

COMMAND = AskOperatorCommand.model_validate(
    {
        "type": "ask_operator",
        "questions": [
            {
                "id": "cleanup",
                "question": "Delete the stale artifacts?",
                "header": "Confirm",
                "options": [
                    {"label": "Yes, delete", "description": "removes three", "recommended": True},
                    {"label": "No, keep"},
                ],
            }
        ],
    }
)


def make_service(tmp_path: Path) -> tuple[OperatorQuestionService, str]:
    database = Database(tmp_path / "questions.db")
    operations = OperationService(OperationRepository(database))
    operation = operations.create(
        "lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("example.com",))
    )
    return OperatorQuestionService(operations, QuestionRepository(database)), operation.id


def test_an_answer_arrives_before_the_deadline(tmp_path: Path) -> None:
    service, operation_id = make_service(tmp_path)
    seen: list[str] = []

    def notify(call_id: str) -> None:
        seen.append(call_id)
        service.answer(call_id, [Answer(id="cleanup", selected=("Yes, delete",))], "op")

    outcome = service.ask(operation_id, "run_1", COMMAND, notify=notify)
    assert outcome.pending is False
    assert json.loads(outcome.text) == {
        "answers": [{"id": "cleanup", "selected": ["Yes, delete"], "custom": ""}]
    }
    assert seen


def test_nobody_answers_and_the_run_keeps_going(tmp_path: Path) -> None:
    service, operation_id = make_service(tmp_path)
    outcome = service.ask(
        operation_id,
        "run_1",
        AskOperatorCommand.model_validate({**COMMAND.model_dump(mode="json"), "timeout": 1}),
    )
    assert outcome.pending is True
    payload = json.loads(outcome.text)
    assert payload["call_id"].startswith("qcall_")
    assert "samurai agent answer" in payload["hint"]
    # the question stays listed so a late answer is still possible
    assert service.pending(operation_id)


def test_a_late_answer_is_accepted_and_recorded(tmp_path: Path) -> None:
    service, operation_id = make_service(tmp_path)
    outcome = service.ask(
        operation_id,
        "run_1",
        AskOperatorCommand.model_validate({**COMMAND.model_dump(mode="json"), "timeout": 1}),
    )
    rows = service.answer(
        outcome.call_id, [Answer(id="cleanup", selected=("No, keep",))], "op"
    )
    assert rows[0].status == "answered"
    assert json.loads(rows[0].answer)["selected"] == ["No, keep"]
    with pytest.raises(OperatorQuestionError, match="already answered"):
        service.answer(outcome.call_id, [Answer(id="cleanup")], "op")


def test_a_free_form_answer_needs_no_option(tmp_path: Path) -> None:
    service, operation_id = make_service(tmp_path)
    seen: list[str] = []

    def notify(call_id: str) -> None:
        seen.append(call_id)
        service.answer(call_id, [Answer(id="cleanup", custom="только логи")], "op")

    outcome = service.ask(operation_id, "run_1", COMMAND, notify=notify)
    assert json.loads(outcome.text)["answers"][0]["custom"] == "только логи"


def test_an_unknown_option_is_refused(tmp_path: Path) -> None:
    service, operation_id = make_service(tmp_path)

    def notify(call_id: str) -> None:
        with pytest.raises(OperatorQuestionError, match="no such option"):
            service.answer(call_id, [Answer(id="cleanup", selected=("Maybe",))], "op")
        service.answer(call_id, [Answer(id="cleanup", selected=("Yes, delete",))], "op")

    assert service.ask(operation_id, "run_1", COMMAND, notify=notify).pending is False


def test_duplicate_ids_and_labels_are_refused(tmp_path: Path) -> None:
    service, operation_id = make_service(tmp_path)
    duplicate = AskOperatorCommand.model_validate(
        {
            "type": "ask_operator",
            "timeout": 1,
            "questions": [
                {"id": "a", "question": "one?", "options": [{"label": "x"}, {"label": "x"}]}
            ],
        }
    )
    with pytest.raises(OperatorQuestionError, match="duplicate option"):
        service.ask(operation_id, "run_1", duplicate)
    twice = AskOperatorCommand.model_validate(
        {
            "type": "ask_operator",
            "timeout": 1,
            "questions": [
                {"id": "a", "question": "one?", "options": [{"label": "x"}, {"label": "y"}]},
                {"id": "a", "question": "two?", "options": [{"label": "x"}, {"label": "y"}]},
            ],
        }
    )
    with pytest.raises(OperatorQuestionError, match="duplicate question"):
        service.ask(operation_id, "run_1", twice)


def test_an_answer_from_another_process_releases_the_wait(tmp_path: Path) -> None:
    """The waiting side polls the store: a second process owns the answer."""
    import threading
    import time

    from samurai_ai.storage.database import Database

    service, operation_id = make_service(tmp_path)
    other = OperatorQuestionService(
        service.operations, QuestionRepository(Database(tmp_path / "questions.db"))
    )
    assert other.questions is not service.questions

    def operator() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = other.pending(operation_id)
            if rows:
                other.answer(rows[0].call_id, [Answer(id="cleanup", selected=("No, keep",))], "op")
                return
            time.sleep(0.02)

    thread = threading.Thread(target=operator, daemon=True)
    thread.start()
    outcome = service.ask(operation_id, "run_1", COMMAND)
    thread.join(5)
    assert outcome.pending is False
    assert json.loads(outcome.text)["answers"][0]["selected"] == ["No, keep"]


class _FakeController:
    """Just enough of the TUI controller to exercise the answer assembly."""

    def __init__(self, orchestrator, operation, picked: dict[str, str | None]) -> None:
        self.orchestrator = orchestrator
        self.operation = operation
        self.picked = picked
        self.written: list[str] = []
        self.translator = type(
            "T", (), {"t": staticmethod(lambda key, **kw: f"{key}:{kw}")}
        )()

    def _pick_option(self, question):  # type: ignore[no-untyped-def]
        return self.picked[question.id]

    def _write_dim(self, message: str) -> None:
        self.written.append(message)

    def _spawn_error_text(self, exc: Exception) -> str:
        return str(exc)


class _FakeOrchestrator:
    def __init__(self, service) -> None:
        self.questions = service


def test_the_screen_path_turns_picks_into_answers(tmp_path: Path) -> None:
    from samurai_ai.cli import main as _main  # noqa: F401  (kept for symmetry)
    from samurai_ai.tui.sessions import SessionController

    service, operation_id = make_service(tmp_path)
    operation = service.operations.get(operation_id)
    controller = _FakeController(
        _FakeOrchestrator(service), operation, {"cleanup": "Yes, delete"}
    )
    seen: list[str] = []

    def notify(call_id: str) -> None:
        seen.append(call_id)
        SessionController._ask_questions(controller, call_id)

    outcome = service.ask(operation_id, "run_1", COMMAND, notify=notify)
    assert outcome.pending is False
    assert json.loads(outcome.text)["answers"][0]["selected"] == ["Yes, delete"]
    assert not controller.written


def test_a_skipped_question_is_reported_and_stays_open(tmp_path: Path) -> None:
    from samurai_ai.tui.sessions import SessionController

    service, operation_id = make_service(tmp_path)
    operation = service.operations.get(operation_id)
    controller = _FakeController(_FakeOrchestrator(service), operation, {"cleanup": None})

    def notify(call_id: str) -> None:
        SessionController._ask_questions(controller, call_id)

    outcome = service.ask(
        operation_id,
        "run_1",
        AskOperatorCommand.model_validate({**COMMAND.model_dump(mode="json"), "timeout": 1}),
        notify=notify,
    )
    assert outcome.pending is True
    assert controller.written and "question.skipped" in controller.written[0]
