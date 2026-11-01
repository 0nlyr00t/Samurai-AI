"""Traffic evidence: capture a pair, bind it to a finding under a role.

Every binding change bumps the finding's evidence version. A caller that read
version N and reports later is refused when the evidence has moved since — the
report then describes what it saw instead of pretending to cover the new state.
"""

from __future__ import annotations

from datetime import UTC, datetime

from samurai_ai.domain.kernel import Finding
from samurai_ai.domain.traffic import (
    MAX_BINDINGS,
    URL_MAX,
    TrafficBinding,
    TrafficError,
    TrafficNotFoundError,
    TrafficPair,
    TrafficRole,
    clip_body,
    digest,
    segment,
)
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.traffic import TrafficRepository


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TrafficService:
    def __init__(
        self, operations: OperationService, kernel: KernelService, traffic: TrafficRepository
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.traffic = traffic

    # --- capture ----------------------------------------------------------
    def record(
        self,
        operation_id: str,
        method: str,
        url: str,
        status: int,
        request_body: str = "",
        response_body: str = "",
        *,
        created_by: str = "agent",
    ) -> TrafficPair:
        self.operations.get(operation_id)
        method = method.strip().upper()[:16]
        url = " ".join(url.split())
        if not method:
            raise TrafficError("a traffic pair needs a method")
        if not url:
            raise TrafficError("a traffic pair needs a URL")
        if len(url) > URL_MAX:
            raise TrafficError(f"URL is longer than {URL_MAX} characters")
        body_in, cut_in = clip_body(request_body)
        body_out, cut_out = clip_body(response_body)
        pair = TrafficPair(
            operation_id=operation_id,
            method=method,
            url=url,
            status=int(status),
            request_body=body_in,
            response_body=body_out,
            truncated=cut_in or cut_out,
            sha256=digest(method, url, status, body_in, body_out),
            created_by=created_by,
        )
        self.traffic.add(pair)
        return pair

    def get(self, pair_id: str) -> TrafficPair:
        pair = self.traffic.get(pair_id)
        if pair is None:
            raise TrafficNotFoundError(f"traffic pair not found: {pair_id}")
        return pair

    def for_operation(self, operation_id: str) -> list[TrafficPair]:
        return self.traffic.for_operation(operation_id)

    def read(self, pair_id: str, side: str, offset: int = 0) -> tuple[str, int]:
        """One segment of a body, with the offset to continue from or 0 at the end."""
        return segment(self.get(pair_id), "request" if side == "request" else "response", offset)

    # --- binding ----------------------------------------------------------
    def bind(
        self,
        finding_id: str,
        traffic_id: str,
        role: TrafficRole,
        note: str = "",
    ) -> Finding:
        finding = self.kernel.kernel.get_finding(finding_id)
        if finding is None:
            raise TrafficError(f"finding not found: {finding_id}")
        pair = self.get(traffic_id)
        if pair.operation_id != finding.operation_id:
            raise TrafficError(f"traffic {traffic_id} belongs to another operation")
        if self.traffic.count_bindings(finding_id) >= MAX_BINDINGS:
            raise TrafficError(f"a finding takes at most {MAX_BINDINGS} traffic bindings")
        if not self.traffic.bind(finding_id, traffic_id, role, " ".join(note.split()), _now()):
            return finding  # already bound, nothing moved
        return self._bump(finding_id)

    def unbind(self, finding_id: str, traffic_id: str) -> Finding:
        if not self.traffic.unbind(finding_id, traffic_id):
            raise TrafficError(f"traffic {traffic_id} is not bound to {finding_id}")
        return self._bump(finding_id)

    def bindings(self, finding_id: str) -> list[TrafficBinding]:
        return self.traffic.bindings(finding_id)

    def _bump(self, finding_id: str) -> Finding:
        finding = self.kernel.kernel.get_finding(finding_id)
        assert finding is not None
        return self.kernel.bump_evidence_version(finding_id)

    # --- report guard -----------------------------------------------------
    def assert_fresh(self, finding_ids: tuple[str, ...], version: int) -> None:
        """Refuse a report whose read version no longer matches the evidence."""
        current = 0
        for finding_id in finding_ids:
            finding = self.kernel.kernel.get_finding(finding_id)
            if finding is not None:
                current = max(current, finding.evidence_version)
        if version != current:
            raise TrafficError(
                f"evidence moved: report was written against version {version}, "
                f"current is {current} — re-read and regenerate"
            )
