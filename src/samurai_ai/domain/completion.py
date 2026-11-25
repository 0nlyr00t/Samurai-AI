"""One completion contract: what the finish line demands and when a goal is met.

The finish line is the condition a goal must satisfy. It is mostly prose for the
judge, but it may also carry deterministic requirements, one per line::

    Obtain a shell on the web host and read user.txt.
    require: finding>=high
    require: artifact=file

Unless the finish line says otherwise, a goal never completes on words alone:
the store must hold proof recorded since the goal began (a confirmed fact or a
live finding, see ``domain/evidence.py``). ``require: none`` is the operator's
explicit waiver for a goal that is an answer rather than a result — then the
judge alone decides. The LLM judge decides only whether that
proof answers the condition; it can veto completion, never grant it without
evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from samurai_ai.domain.evidence import artifact_is_bound, proven_facts
from samurai_ai.domain.goal import Goal
from samurai_ai.domain.graph import GraphNode
from samurai_ai.domain.kernel import (
    Artifact,
    ArtifactKind,
    Finding,
    FindingSeverity,
    FindingStatus,
)

_SEVERITY_ORDER = tuple(FindingSeverity)
#: findings the operator or validation already threw out prove nothing
_DEAD_FINDINGS = frozenset({FindingStatus.FALSE_POSITIVE, FindingStatus.DUPLICATE})
_REQUIRE = "require:"


class CompletionCriterionError(ValueError):
    pass


@dataclass(frozen=True)
class CompletionCriterion:
    """The finish line, split into judge prose and machine-checked demands."""

    text: str
    min_finding: FindingSeverity | None = None
    artifact_kinds: tuple[ArtifactKind, ...] = ()
    #: False only when the operator waived proof with ``require: none``
    evidence_required: bool = True

    @classmethod
    def parse(cls, raw: str) -> CompletionCriterion:
        prose: list[str] = []
        min_finding: FindingSeverity | None = None
        kinds: list[ArtifactKind] = []
        evidence_required = True
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith(_REQUIRE):
                prose.append(line)
                continue
            demand = stripped[len(_REQUIRE) :].strip().lower().replace(" ", "")
            if demand == "none":
                evidence_required = False
            elif demand.startswith("finding>="):
                try:
                    min_finding = FindingSeverity(demand.removeprefix("finding>="))
                except ValueError as exc:
                    raise CompletionCriterionError(f"unknown severity in {stripped!r}") from exc
            elif demand.startswith("artifact="):
                try:
                    kinds.append(ArtifactKind(demand.removeprefix("artifact=")))
                except ValueError as exc:
                    raise CompletionCriterionError(
                        f"unknown artifact kind in {stripped!r}"
                    ) from exc
            else:
                raise CompletionCriterionError(
                    f"unknown requirement {stripped!r}: use finding>=<severity>, "
                    "artifact=<kind> or none"
                )
        if not evidence_required and (min_finding is not None or kinds):
            raise CompletionCriterionError("require: none cannot be combined with other demands")
        return cls(
            "\n".join(prose).strip(),
            min_finding,
            tuple(dict.fromkeys(kinds)),
            evidence_required,
        )


def completion_criterion(goal: Goal | None, operation_goal: str = "") -> CompletionCriterion:
    """The finish line wins, then the goal's objective, then the operation's goal."""
    if goal is not None and goal.finish_line.strip():
        criterion = CompletionCriterion.parse(goal.finish_line)
        if not criterion.text:
            # requirements alone still need words for the judge
            return CompletionCriterion(
                goal.objective.strip(),
                criterion.min_finding,
                criterion.artifact_kinds,
                criterion.evidence_required,
            )
        return criterion
    if goal is not None and goal.objective.strip():
        return CompletionCriterion(goal.objective.strip())
    return CompletionCriterion(operation_goal.strip())


@dataclass(frozen=True)
class CompletionEvidence:
    """What the store proves since the goal began."""

    facts: tuple[GraphNode, ...]
    findings: tuple[Finding, ...]
    artifact_kinds: frozenset[ArtifactKind]

    @classmethod
    def collect(
        cls,
        nodes: Iterable[GraphNode],
        findings: Iterable[Finding],
        artifacts: Iterable[Artifact],
        *,
        since: datetime | None = None,
    ) -> CompletionEvidence:
        def fresh(created_at: datetime) -> bool:
            return since is None or created_at >= since

        return cls(
            facts=proven_facts(nodes, since=since),
            findings=tuple(
                finding
                for finding in findings
                if finding.status not in _DEAD_FINDINGS
                and finding.evidence_ids
                and fresh(finding.created_at)
            ),
            artifact_kinds=frozenset(
                artifact.kind
                for artifact in artifacts
                if artifact_is_bound(artifact) and fresh(artifact.created_at)
            ),
        )

    def digest(self, limit: int = 1500) -> str:
        """The proof the judge may rely on — store records, not conversation."""
        lines = [
            f"- fact {fact.id}: {fact.text('summary')[:200]}"
            + (" (negative)" if fact.payload.get("result") == "negative" else "")
            for fact in self.facts[-8:]
        ]
        lines += [
            f"- finding {finding.id} [{finding.severity.value}]: {finding.title}"
            for finding in self.findings[-8:]
        ]
        text = "\n".join(lines) or "(no proven facts or findings yet)"
        return text[:limit]


def missing_requirements(
    criterion: CompletionCriterion, evidence: CompletionEvidence
) -> tuple[str, ...]:
    """Every deterministic demand the store does not meet yet; empty means met."""
    missing: list[str] = []
    if criterion.evidence_required and not evidence.facts and not evidence.findings:
        missing.append(
            "no proven fact or finding since the goal started — cite the run_ids or "
            "artifact_ids that prove the result"
        )
    if criterion.min_finding is not None:
        floor = _SEVERITY_ORDER.index(criterion.min_finding)
        if not any(
            _SEVERITY_ORDER.index(finding.severity) >= floor for finding in evidence.findings
        ):
            missing.append(f"no evidenced finding of severity {criterion.min_finding.value}+")
    missing += [
        f"no bound {kind.value} artifact"
        for kind in criterion.artifact_kinds
        if kind not in evidence.artifact_kinds
    ]
    return tuple(missing)


class CompletionDecision(StrEnum):
    COMPLETE = "complete"
    #: the judge saw no proof that the condition holds
    CONTINUE = "continue"
    #: the judge agreed, but the store lacks the proof
    UNPROVEN = "unproven"
    #: no judge answer at all
    UNAVAILABLE = "unavailable"


def decide(verdict: str | None, missing: tuple[str, ...]) -> CompletionDecision:
    """Combine the judge's verdict with the store check; evidence can only veto."""
    if verdict is None:
        return CompletionDecision.UNAVAILABLE
    if verdict != "stop":
        return CompletionDecision.CONTINUE
    return CompletionDecision.UNPROVEN if missing else CompletionDecision.COMPLETE


__all__ = [
    "CompletionCriterion",
    "CompletionCriterionError",
    "CompletionDecision",
    "CompletionEvidence",
    "completion_criterion",
    "decide",
    "missing_requirements",
]
