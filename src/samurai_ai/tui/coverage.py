"""Methodology coverage: which surface and which check classes have evidence.

Coverage is not the number of commands run. A host counts as covered when work
actually reached it (a successful run named it); a check class counts as
covered when a task of that class is done. Everything else carries the reason
it is not: excluded by scope, blocked by a task, or simply still pending — and
a class the plan never asked for is marked not-applicable instead of silently
counting as covered.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from samurai_ai.domain.kernel import PlanTask, TaskStatus
from samurai_ai.tui.hunt import CATEGORIES, DONE_STATES, category_of


class CoverageState(StrEnum):
    COVERED = "covered"
    PENDING = "pending"
    NOT_APPLICABLE = "not-applicable"
    BLOCKED_SCOPE = "blocked-by-scope"
    BLOCKED_POLICY = "blocked-by-policy"


MARKERS = {
    CoverageState.COVERED: "✓",
    CoverageState.PENDING: "·",
    CoverageState.NOT_APPLICABLE: "—",
    CoverageState.BLOCKED_SCOPE: "⇥",
    CoverageState.BLOCKED_POLICY: "⛔",
}


@dataclass(frozen=True)
class CoverageRow:
    """One surface host or check class and the reason behind its state."""

    kind: str
    target: str
    state: CoverageState
    reason: str = ""


def surface_rows(
    targets: Sequence[str], touched: Iterable[str], excluded: Iterable[str] = ()
) -> list[CoverageRow]:
    """Per scope target: reached, still pending, or out of scope and why."""
    hit = {host.lower() for host in touched}
    out = {rule.lower() for rule in excluded}
    rows: list[CoverageRow] = []
    for target in targets:
        if target.lower() in out:
            rows.append(CoverageRow("host", target, CoverageState.BLOCKED_SCOPE, target))
        elif target.lower() in hit:
            rows.append(CoverageRow("host", target, CoverageState.COVERED))
        else:
            rows.append(CoverageRow("host", target, CoverageState.PENDING))
    return rows


def check_rows(tasks: Sequence[PlanTask]) -> list[CoverageRow]:
    """Per check class the plan knows: done, blocked with its task, or pending."""
    by_class: dict[str, list[PlanTask]] = {key: [] for key in CATEGORIES}
    for task in tasks:
        key = category_of(task.title)
        if key is not None:
            by_class[key].append(task)
    rows: list[CoverageRow] = []
    for key, own in by_class.items():
        if not own:
            rows.append(CoverageRow("check", key, CoverageState.NOT_APPLICABLE))
            continue
        blocked = [task for task in own if task.status is TaskStatus.BLOCKED]
        if blocked:
            rows.append(CoverageRow("check", key, CoverageState.BLOCKED_POLICY, blocked[0].title))
            continue
        if all(task.status in DONE_STATES for task in own):
            rows.append(CoverageRow("check", key, CoverageState.COVERED))
            continue
        rows.append(CoverageRow("check", key, CoverageState.PENDING))
    return rows


def summarise(rows: Sequence[CoverageRow]) -> tuple[int, int]:
    """(done, total) over the rows that were actually on the table."""
    counted = [row for row in rows if row.state is not CoverageState.NOT_APPLICABLE]
    done = sum(1 for row in counted if row.state is CoverageState.COVERED)
    return done, len(counted)
