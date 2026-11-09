"""The progress tab counts plan tasks and keeps the findings that matter visible."""

from datetime import UTC, datetime, timedelta

from samurai_ai.domain.kernel import Finding, FindingSeverity, FindingStatus, PlanTask, TaskStatus
from samurai_ai.tui.hunt import bar, category_of, percent, ranked, tally

_SERIAL = iter(range(100))


def task(title: str, status: TaskStatus = TaskStatus.PENDING) -> PlanTask:
    return PlanTask(id=f"t-{next(_SERIAL)}", title=title, status=status)


def finding(severity: FindingSeverity, status: FindingStatus = FindingStatus.CANDIDATE,
            age: int = 0) -> Finding:
    item = Finding(operation_id="op_1", title=f"{severity.value} one", severity=severity)
    item.status = status
    item.created_at = datetime.now(UTC) - timedelta(minutes=age)
    return item


def test_titles_are_sorted_into_the_three_hunts() -> None:
    assert category_of("Перебор паролей через hydra") == "brute"
    assert category_of("Фаззинг каталогов через ffuf") == "fuzz"
    assert category_of("Password spray на OWA") == "spray"
    assert category_of("Разведка поддоменов") is None


def test_only_hunt_tasks_are_counted_and_done_means_done() -> None:
    tasks = [
        task("Брутфорс SSH", TaskStatus.DONE),
        task("Брутфорс панели", TaskStatus.IN_PROGRESS),
        task("Разведка", TaskStatus.DONE),
    ]
    assert tally(tasks) == {"brute": (1, 2)}


def test_an_empty_category_list_is_not_a_division_by_zero() -> None:
    assert percent(0, 0) == 100
    assert bar(0, 0) == "█" * 12
    assert percent(1, 4) == 25
    assert bar(1, 4).count("█") == 3


def test_findings_order_by_severity_then_newest_and_drop_the_dropped() -> None:
    items = [
        finding(FindingSeverity.LOW),
        finding(FindingSeverity.CRITICAL, age=10),
        finding(FindingSeverity.HIGH),
        finding(FindingSeverity.CRITICAL, age=1),
        finding(FindingSeverity.CRITICAL, status=FindingStatus.FALSE_POSITIVE),
        finding(FindingSeverity.INFO, status=FindingStatus.DUPLICATE),
    ]
    order = [(item.severity.value, item.created_at) for item in ranked(items)]
    assert [severity for severity, _ in order] == ["critical", "critical", "high", "low"]
    # the newer critical comes first inside the severity
    assert order[1][1] > order[0][1]


def test_the_list_is_bounded() -> None:
    items = [finding(FindingSeverity.HIGH) for _ in range(20)]
    assert len(ranked(items)) == 8


def test_hypothesis_score_priors_and_evidence_boost() -> None:
    from samurai_ai.tui.hunt import hypothesis_score

    strong = hypothesis_score("SSTI в заметках билета", [])
    weak = hypothesis_score("что-то экзотическое", [])
    assert strong > weak
    boosted = hypothesis_score(
        "SSTI в заметках билета", ["заметки билета рендерятся через Go template"]
    )
    assert boosted > strong
    assert hypothesis_score("SSTI в заметках билета", ["факт"] * 100) <= 0.99
