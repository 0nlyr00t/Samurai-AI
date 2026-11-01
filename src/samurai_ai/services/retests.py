"""Retests with a verdict contract.

`fixed` is the expensive verdict: it needs evidence *and* a control that still
works, because "the payload stopped working" is also what a broken environment
looks like. Anything short of the bar is `inconclusive`, which is recorded rather
than hidden.
"""

from __future__ import annotations

from datetime import UTC, datetime

from samurai_ai.domain.kernel import Finding, FindingStatus
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.retest import (
    EVIDENCE_MAX,
    SUMMARY_MAX,
    Retest,
    RetestError,
    RetestNotFoundError,
    RetestVerdict,
)
from samurai_ai.domain.traffic import TrafficNotFoundError, TrafficRole
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.traffic import TrafficService
from samurai_ai.storage.retests import RetestRepository

#: Findings far enough along to be worth re-checking.
RETESTABLE = (FindingStatus.VALIDATED, FindingStatus.REPORTED)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RetestService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        retests: RetestRepository,
        traffic: TrafficService,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.retests = retests
        self.traffic = traffic

    def start(self, finding_id: str, *, created_by: str = "operator") -> Retest:
        finding = self._finding(finding_id)
        if finding.status not in RETESTABLE:
            raise RetestError(
                f"finding {finding_id} is {finding.status.value}; "
                f"only validated or reported findings are re-tested"
            )
        if self.retests.open_for_finding(finding_id) is not None:
            raise RetestError(f"finding {finding_id} already has an open retest")
        retest = Retest(
            operation_id=finding.operation_id, finding_id=finding_id, created_by=created_by
        )
        self.retests.add(retest)
        self._event(finding.operation_id, "retest.started", retest)
        return retest

    def finish(
        self,
        retest_id: str,
        verdict: RetestVerdict,
        summary: str,
        evidence: str,
        control: str = "",
    ) -> Retest:
        retest = self.get(retest_id)
        if not retest.open:
            raise RetestError(f"retest {retest_id} already has a verdict: {retest.verdict}")
        summary = " ".join(summary.split())[:SUMMARY_MAX]
        evidence = evidence.strip()[:EVIDENCE_MAX]
        control = " ".join(control.split())[:SUMMARY_MAX]
        if not summary:
            raise RetestError("a verdict needs a summary of what was actually checked")
        if not evidence:
            raise RetestError(
                f"verdict {verdict.value} needs evidence — requests, responses or command output"
            )
        control_pair = ""
        if verdict is RetestVerdict.FIXED and not control:
            raise RetestError(
                "verdict fixed needs a control: a comparable request that still behaves normally"
            )
        if control:
            control_pair = self._control_pair(retest.finding_id, control)
        finished = self.retests.finish(
            retest_id, verdict, summary, evidence, control, control_pair, _now()
        )
        if finished is None:
            raise RetestError(f"retest {retest_id} was finished concurrently")
        self._event(finished.operation_id, "retest.finished", finished)
        if verdict is RetestVerdict.FIXED:
            self.kernel.mark_fixed(finished.finding_id)
        return finished

    def _control_pair(self, finding_id: str, control: str) -> str:
        """A control that names a captured pair gets checked and bound; text stays text."""
        if not control.startswith("trf_"):
            return ""
        try:
            pair = self.traffic.get(control)
        except TrafficNotFoundError:
            return ""
        finding = self._finding(finding_id)
        if pair.operation_id != finding.operation_id:
            raise RetestError(f"control pair {control} belongs to another operation")
        self.traffic.bind(finding_id, pair.id, TrafficRole.VERIFICATION)
        return pair.id

    def get(self, retest_id: str) -> Retest:
        retest = self.retests.get(retest_id)
        if retest is None:
            raise RetestNotFoundError(f"retest not found: {retest_id}")
        return retest

    def for_finding(self, finding_id: str) -> list[Retest]:
        return self.retests.for_finding(finding_id)

    def _finding(self, finding_id: str) -> Finding:
        finding = self.kernel.kernel.get_finding(finding_id)
        if finding is None:
            raise RetestError(f"finding not found: {finding_id}")
        return finding

    def _event(self, operation_id: str, event_type: str, retest: Retest) -> None:
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=event_type,
                payload={
                    "retest_id": retest.id,
                    "finding_id": retest.finding_id,
                    "verdict": retest.verdict.value if retest.verdict else "",
                },
            )
        )
