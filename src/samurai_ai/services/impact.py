"""Impact escalation service: bounded attempts, journal-backed state."""

from __future__ import annotations

from samurai_ai.domain.impact import (
    ImpactAttempt,
    ImpactError,
    ImpactLevel,
    ImpactState,
    validate_attempt,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService


class ImpactService:
    """Escalation lives in the journal: state is derived, never a side table."""

    def __init__(self, operations: OperationService, kernel: KernelService) -> None:
        self.operations = operations
        self.kernel = kernel

    def state(self, finding_id: str) -> ImpactState:
        finding = self.kernel.get_finding_by_id(finding_id)
        state = ImpactState(finding_id=finding_id)
        # the budget is spent for real: attempts recorded in the journal count
        # against it, so a resumed session cannot start over for free
        for event in self.operations.events(finding.operation_id):
            if event.event_type != "impact.attempt":
                continue
            if event.payload.get("finding_id") != finding_id:
                continue
            state.budget.attempts += 1
            state.attempts.append(
                ImpactAttempt(
                    finding_id=finding_id,
                    level=ImpactLevel(event.payload["level"]),
                    evidence=tuple(event.payload.get("evidence") or ()),
                    outcome=str(event.payload.get("outcome", "confirmed")),
                    rollback=str(event.payload.get("rollback", "")),
                )
            )
        return state

    def record(
        self,
        finding_id: str,
        level: ImpactLevel | str,
        evidence: tuple[str, ...],
        *,
        rollback: str = "",
        outcome: str = "confirmed",
        requests: int = 1,
        seconds: float = 0.0,
    ) -> ImpactState:
        finding = self.kernel.get_finding_by_id(finding_id)
        attempt = validate_attempt(
            ImpactAttempt(
                finding_id=finding_id,
                level=ImpactLevel(level),
                evidence=tuple(evidence),
                outcome=outcome,
                rollback=rollback,
            )
        )
        state = self.state(finding_id)
        budget = state.budget
        budget.spend(requests=requests, seconds=seconds)
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=finding.operation_id,
                event_type="impact.attempt",
                payload={
                    "finding_id": finding_id,
                    "level": attempt.level.value,
                    "outcome": attempt.outcome,
                    "evidence": list(attempt.evidence),
                    "rollback": attempt.rollback,
                    "points": attempt.level.points,
                },
            )
        )
        if attempt.outcome == "confirmed":
            # the evidence set moved: reports must not go out against a stale one
            self.kernel.bump_evidence_version(finding_id)
        return self.state(finding_id)

    def grade(self, finding_id: str) -> ImpactState:
        state = self.state(finding_id)
        if state.level is None:
            raise ImpactError(
                f"finding {finding_id} has no confirmed impact level yet; "
                "validate first, then escalate"
            )
        return state

    def suggest(self, finding_id: str) -> ImpactLevel | None:
        return self.state(finding_id).next_level()
