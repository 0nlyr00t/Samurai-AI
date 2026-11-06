"""The hunt tab: how far the repetitive searches got, and what they found.

Brute force, content discovery and password spraying are long and boring; the
operator asking "how far along is it" used to mean reading the journal. The tab
answers with the plan's own tasks — the only denominator that reliably exists —
and keeps the findings that matter in sight as they land.

Nothing here decides anything: it counts statuses the kernel already holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text

from samurai_ai.domain.kernel import Finding, FindingStatus
from samurai_ai.tui.journal import entity_span

if TYPE_CHECKING:
    from samurai_ai.domain.kernel import PlanTask


#: category → words that put a task in it (lower-case, matched as substrings)
CATEGORIES: dict[str, tuple[str, ...]] = {
    "brute": ("брутфорс", "bruteforce", "brute force", "brute-force", "hydra", "медуза", "medusa"),
    "fuzz": ("фаззинг", "fuzz", "ffuf", "gobuster", "feroxbuster", "dirb", "словaр", "wordlist"),
    "spray": ("спрей", "spray", "password spray", "перебор паролей", "credential stuffing"),
}

DONE_STATES = {"done", "skipped"}
#: how the findings block orders itself: severity first, then the impact ladder
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
FINDINGS_LIMIT = 8
#: the stats move slowly: a 3-minute cadence keeps the render cheap
REFRESH_SECONDS = 180


def category_of(title: str) -> str | None:
    """Which hunt category a task title belongs to, if any."""
    lowered = title.lower()
    for key, words in CATEGORIES.items():
        if any(word in lowered for word in words):
            return key
    return None


def tally(tasks: list[PlanTask]) -> dict[str, tuple[int, int]]:
    """(done, total) per category, counting only the tasks that belong to one."""
    counted: dict[str, tuple[int, int]] = {}
    for task in tasks:
        key = category_of(task.title)
        if key is None:
            continue
        done, total = counted.get(key, (0, 0))
        counted[key] = (done + (1 if task.status.value in DONE_STATES else 0), total + 1)
    return counted


def percent(done: int, total: int) -> int:
    return 100 if not total else round(done * 100 / total)


def bar(done: int, total: int, width: int = 12) -> str:
    """A text progress bar; full when there is nothing to do."""
    filled = width if not total else round(done * width / total)
    return "█" * filled + "░" * (width - filled)


def ranked(findings: list[Finding]) -> list[Finding]:
    """The findings worth showing first: severity, then newest within a severity.

    A dropped finding (false positive, duplicate) leaves the block: it would only
    pad a list the operator reads at a glance.
    """
    live = [
        item
        for item in findings
        if item.status not in (FindingStatus.FALSE_POSITIVE, FindingStatus.DUPLICATE)
    ]
    return sorted(
        live,
        key=lambda item: (SEVERITY_RANK.get(item.severity.value, 9), item.created_at.timestamp()),
        reverse=False,
    )[:FINDINGS_LIMIT]


class HuntController:
    """Rendering for the hunt tab; mixed into the app."""

    if TYPE_CHECKING:
        operation: Any
        kernel: Any
        translator: Any

        def paint(self, token: str) -> str: ...
        def _write_dim(self, text: str, *, process: bool = True) -> None: ...
        def query_one(self, *args: Any, **kwargs: Any) -> Any: ...

    def _hunt_tasks(self) -> list[PlanTask]:
        if self.operation is None:
            return []
        plan = self.kernel.get_current_plan(self.operation.id)
        return list(plan.tasks) if plan is not None else []

    def _hunt_findings(self) -> list[Finding]:
        if self.operation is None:
            return []
        return list(self.kernel.kernel.list_findings(self.operation.id))

    def _hunt_lines(self) -> list[str | Text]:
        if self.operation is None:
            return [
                Text(self.translator.t("status.no-operation"), style=self.paint("dim text-dim"))
            ]
        lines: list[str | Text] = [
            Text(self.translator.t("hunt.title-stats"), style=self.paint("bold $accent"))
        ]
        counts = tally(self._hunt_tasks())
        if not counts:
            lines.append(
                Text(self.translator.t("hunt.no-searches"), style=self.paint("dim text-dim"))
            )
        for key, (done, total) in sorted(counts.items()):
            lines.append(
                Text.assemble(
                    (f"{self.translator.t(f'hunt.category.{key}')}  ", self.paint("$text")),
                    (bar(done, total), self.paint("$accent")),
                    (f"  {percent(done, total)}%", self.paint("bold $accent")),
                    (f"  {done}/{total}", self.paint("dim text-dim")),
                )
            )
        lines.append(Text())
        lines.append(
            Text(self.translator.t("hunt.title-findings"), style=self.paint("bold $accent"))
        )
        findings = ranked(self._hunt_findings())
        if not findings:
            lines.append(
                Text(self.translator.t("hunt.no-findings"), style=self.paint("dim text-dim"))
            )
        for finding in findings:
            lines.append(
                Text.assemble(
                    (f"{finding.severity.value:<8}", self.paint(f"$risk-{_risk_token(finding)}")),
                    (finding.title, self.paint("$text")),
                    ("  ", ""),
                    entity_span(
                        finding.id, "finding", finding.id, base_style=self.paint("dim text-dim")
                    ),
                )
            )
        lines.append(Text())
        lines.append(
            Text(
                self.translator.t("hunt.hint", shortcut="Ctrl-T"),
                style=self.paint("dim text-faint"),
            )
        )
        return lines

    def refresh_hunt(self) -> None:
        """Re-render the tab if it is the visible pane."""
        switcher: Any = self.query_one("#views")
        if str(switcher.current or "") != "view-hunt":
            return
        log: Any = self.query_one("#view-hunt")
        log.set_lines(self._hunt_lines())


def _risk_token(finding: Finding) -> str:
    return {
        "critical": "high",
        "high": "high",
        "medium": "mid",
        "low": "low",
        "info": "low",
    }.get(finding.severity.value, "mid")
