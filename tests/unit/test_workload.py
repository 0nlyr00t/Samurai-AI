"""Narrow workload classification for document tasks and auxiliary agents."""

from samurai_ai.domain.workload import is_document_objective


def test_document_questions_are_not_attack_objectives() -> None:
    assert is_document_objective(
        "Go to https://agentreadingtest.com/start/ and follow the instructions"
    )
    assert is_document_objective("Read the documentation and answer all 10 questions")
    assert is_document_objective("Прочитай документацию и ответь на вопросы")


def test_security_reading_is_not_misclassified_as_document_qa() -> None:
    assert not is_document_objective("Get a shell and read user.txt")
    assert not is_document_objective("Получи доступ и прочитай user.txt")
    assert not is_document_objective("Find and exploit the SQL injection")
