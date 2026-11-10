"""The hunt tab: how far the repetitive searches got, and what they found.

Brute force, content discovery and password spraying are long and boring; the
operator asking "how far along is it" used to mean reading the journal. The tab
answers with the plan's own tasks — the only denominator that reliably exists —
and keeps the findings that matter in sight as they land.

Nothing here decides anything: it counts statuses the kernel already holds.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.text import Text

from samurai_ai.domain.graph import NodeKind, NodeState
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


#: evidence-free priors by hypothesis class, strongest first
_PRIOR: tuple[tuple[float, tuple[str, ...]], ...] = (
    (0.85, ("ssti", "rce", "sql", "injec", "инъекц", "forg", "поддел", "auth bypass", "обход")),
    (0.65, ("upload", "загрузк", "traversal", "idor", "template", "ssrf", "xxe", "deserial")),
    (0.45, ("default", "дефолт", "weak", "brute", "брут", "spray", "перебор")),
    (0.25, ("misconfig", "конфиг", "exposure", "утечк", "leak", "debug")),
)


def _age_of(node: Any) -> str:
    """How long the hypothesis has been open, coarse enough to scan."""
    created = getattr(node, "created_at", None)
    if created is None:
        return ""
    minutes = max(0, int((datetime.now(UTC) - created).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h" if hours < 24 else f"{hours // 24}d"


def hypothesis_score(title: str, facts: list[str]) -> float:
    """Probability that a hypothesis pays off: class prior × evidence so far.

    Every confirmed fact that shares a content word with the title adds weight;
    the score drives board order and the «most probable first» rule.
    """
    lowered = title.lower()
    prior = next(
        (score for score, markers in _PRIOR if any(m in lowered for m in markers)),
        0.15,
    )
    words = {w for w in re.findall(r"[a-zа-я0-9]{5,}", lowered)}
    boost = 0.0
    for fact in facts:
        fact_words = set(re.findall(r"[a-zа-я0-9]{5,}", fact.lower()))
        if words & fact_words:
            boost += 0.1
    return min(0.99, prior + min(boost, 0.5))


class HuntController:
    """Rendering for the hunt tab; mixed into the app."""

    if TYPE_CHECKING:
        operation: Any
        kernel: Any
        graph: Any
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
        for title, status in getattr(self, "_turn_todo", []):
            mark = {"done": "✓", "in_progress": "▶"}.get(status, "·")
            token = {"done": "success", "in_progress": "risk-mid"}.get(status, "dim text-faint")
            lines.append(Text(f"{mark} {title}", style=self.paint(token)))
        if getattr(self, "_turn_todo", []):
            lines.append(Text())
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
            Text(self.translator.t("hunt.title-hypotheses"), style=self.paint("bold $accent"))
        )
        for row in self._hypothesis_rows():
            line = Text()
            for part in row:
                line.append_text(part)
            lines.append(line)
        if not self._hypothesis_rows():
            lines.append(Text(self.translator.t("hunt.no-hypotheses"),
                              style=self.paint("dim text-faint")))
        lines.append(Text())
        lines.append(
            Text(
                self.translator.t("hunt.hint", shortcut="Ctrl-T"),
                style=self.paint("dim text-faint"),
            )
        )
        return lines

    _HYPOTHESIS_ORDER = ("running", "open", "paused", "blocked", "done", "exhausted")
    _HYPOTHESIS_MARKS = {
        "running": "▶",
        "open": "○",
        "paused": "⏸",
        "blocked": "⛔",
        "done": "✓",
        "exhausted": "✗",
    }

    #: what a hypothesis's evidence says about it, worst case first
    _QUALITY = (
        ("refuted", "hunt.quality.refuted", "danger"),
        ("doubtful", "hunt.quality.doubtful", "risk-mid"),
        ("supported", "hunt.quality.supported", "success"),
    )

    def _hypothesis_rows(self) -> list[list[Text]]:
        """Live hypotheses first, each with its evidence, next test and age.

        The order still uses the class prior × evidence score, but the label is
        qualitative: a percentage from a prior nobody can check reads as more
        certainty than there is.
        """
        if self.operation is None:
            return []
        nodes = list(self.graph.for_operation(self.operation.id))
        facts = [
            node
            for node in nodes
            if node.kind is NodeKind.FACT and node.state is NodeState.CONFIRMED
        ]
        fact_texts = [str(node.payload.get("summary") or node.text()) for node in facts]
        intents = [node for node in nodes if node.kind is NodeKind.INTENT]
        live = {"running", "open"}

        def order_index(node: Any) -> int:
            return (
                self._HYPOTHESIS_ORDER.index(node.state.value)
                if node.state.value in self._HYPOTHESIS_ORDER
                else len(self._HYPOTHESIS_ORDER)
            )

        intents.sort(
            key=lambda node: (
                0 if node.state.value in live else 1,
                -hypothesis_score(node.text(), fact_texts)
                if node.state.value in live
                else order_index(node),
            )
        )
        rows: list[list[Text]] = []
        for node in intents[:12]:
            rows.append(self._hypothesis_line(node, facts))
        return rows

    def _hypothesis_line(self, node: Any, facts: list[Any]) -> list[Text]:
        """One hypothesis: mark, title, evidence counts, plan and age."""
        support, refute = self._evidence_for(node, facts)
        mark = self._HYPOTHESIS_MARKS.get(node.state.value, "?")
        live = node.state.value in ("running", "open")
        quality = self._quality(support, refute) if live else None
        line = [
            Text(f"{mark} ", style=self.paint("$text")),
            Text(node.text(), style=self.paint("$text")),
        ]
        if quality is not None:
            line.append(Text(f"  {self.translator.t(quality[1])}", style=self.paint(quality[2])))
        if support or refute:
            line.append(
                Text(f"  +{support}/−{refute}", style=self.paint("dim text-dim"))
            )
            for fact in self._evidence_links(node, facts)[:3]:
                line.append(Text(" "))
                line.append(entity_span(fact[0][:6], "node", fact[1],
                                       base_style=self.paint("dim text-dim")))
        next_test = str(node.payload.get("next_test") or "")
        if next_test:
            line.append(Text(f"  → {next_test}", style=self.paint("dim text-dim")))
        stop = str(node.payload.get("stop_condition") or "")
        if stop:
            line.append(Text(f"  ⊘ {stop}", style=self.paint("dim text-faint")))
        age = _age_of(node)
        if age:
            stalled = live and node.state.value == "running" and not support and not refute
            line.append(
                Text(
                    f"  {self.translator.t('hunt.stalled' if stalled else 'hunt.age', age=age)}",
                    style=self.paint("warning" if stalled else "dim text-faint"),
                )
            )
        outcome = str(
            node.payload.get("outcome")
            or node.blocked_reason
            or node.payload.get("reason")
            or ""
        )
        if outcome and not live:
            line.append(Text(f"  — {outcome}", style=self.paint("dim text-dim")))
        return line

    def _evidence_links(self, node: Any, facts: list[Any]) -> list[tuple[str, str]]:
        """(label, node id) of the facts behind the hypothesis, for the jump."""
        yielded = {item.id for item in self.graph.facts_of(node.id)}
        links: list[tuple[str, str]] = []
        for fact in facts:
            if fact.id in yielded:
                links.append((fact.text()[:24], fact.id))
        return links

    def _quality(self, support: int, refute: int) -> tuple[str, str, str]:
        if not support and not refute:
            return ("doubtful", "hunt.quality.untested", "dim text-dim")
        if refute > support:
            return self._QUALITY[0]
        if support > refute:
            return self._QUALITY[2]
        return self._QUALITY[1]

    def _evidence_for(self, node: Any, facts: list[Any]) -> tuple[int, int]:
        """Confirmed facts the hypothesis produced, split by polarity."""
        yielded = {item.id for item in self.graph.facts_of(node.id)}
        support = refute = 0
        for fact in facts:
            if fact.id not in yielded:
                continue
            if str(fact.payload.get("result") or "") == "negative":
                refute += 1
            else:
                support += 1
        return support, refute

    def refresh_hunt(self) -> None:
        """Re-render the tab if it is the visible pane."""
        refresh_chain = getattr(self, "_refresh_killchain", None)
        if refresh_chain is not None:
            # the same three-minute beat keeps the stage rail honest
            refresh_chain()
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
