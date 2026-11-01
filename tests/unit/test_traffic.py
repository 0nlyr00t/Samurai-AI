"""Traffic evidence: capped bodies, role bindings, and a moving version."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.kernel import FindingSeverity, PlanTask
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.traffic import BODY_MAX, SEGMENT_MAX, TrafficError, TrafficRole
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.traffic import TrafficService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.traffic import TrafficRepository


@pytest.fixture
def stack(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "traffic.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    traffic = TrafficService(operations, kernel, TrafficRepository(database))
    operation = operations.create("gate", OperationMode.BUG_BOUNTY, ScopeContract(includes=("x",)))
    kernel.create_plan(operation.id, "plan", [PlanTask(id="recon", title="Recon")], [])
    finding = kernel.create_finding(operation.id, "IDOR", FindingSeverity.HIGH)
    return traffic, kernel, operation.id, finding.id


def test_a_pair_is_stored_with_its_hash(stack) -> None:
    traffic, _kernel, operation_id, _finding = stack

    pair = traffic.record(operation_id, "get", "https://x/user/2", 200, "", "{}")

    assert pair.method == "GET"
    assert len(pair.sha256) == 64
    assert traffic.get(pair.id).url == "https://x/user/2"


def test_bodies_are_clipped_and_flagged(stack) -> None:
    traffic, _kernel, operation_id, _finding = stack

    pair = traffic.record(operation_id, "GET", "https://x", 200, "r" * (BODY_MAX + 10), "")

    assert len(pair.request_body) == BODY_MAX
    assert pair.truncated is True


def test_reads_are_segmented(stack) -> None:
    traffic, _kernel, operation_id, _finding = stack
    pair = traffic.record(operation_id, "GET", "https://x", 200, "a" * (SEGMENT_MAX + 5), "")

    first, offset = traffic.read(pair.id, "request")
    second, end = traffic.read(pair.id, "request", offset)

    assert len(first) == SEGMENT_MAX
    assert second == "a" * 5
    assert end == 0


def test_an_empty_url_is_refused(stack) -> None:
    traffic, _kernel, operation_id, _finding = stack

    with pytest.raises(TrafficError):
        traffic.record(operation_id, "GET", "   ", 200)


def test_binding_bumps_the_evidence_version(stack) -> None:
    traffic, kernel, operation_id, finding_id = stack
    pair = traffic.record(operation_id, "GET", "https://x/user/2", 200)

    binding = traffic.bind(finding_id, pair.id, TrafficRole.PROOF, "cross-tenant read")

    assert binding.evidence_version == 1
    assert traffic.bindings(finding_id)[0].role is TrafficRole.PROOF


def test_rebinding_the_same_pair_does_not_bump_again(stack) -> None:
    traffic, _kernel, operation_id, finding_id = stack
    pair = traffic.record(operation_id, "GET", "https://x", 200)
    traffic.bind(finding_id, pair.id, TrafficRole.PROOF)

    again = traffic.bind(finding_id, pair.id, TrafficRole.BASELINE)

    assert again.evidence_version == 1


def test_unbinding_bumps_too(stack) -> None:
    traffic, _kernel, operation_id, finding_id = stack
    pair = traffic.record(operation_id, "GET", "https://x", 200)
    traffic.bind(finding_id, pair.id, TrafficRole.PROOF)

    after = traffic.unbind(finding_id, pair.id)

    assert after.evidence_version == 2
    assert traffic.bindings(finding_id) == []


def test_a_stale_version_is_refused(stack) -> None:
    traffic, _kernel, operation_id, finding_id = stack
    pair = traffic.record(operation_id, "GET", "https://x", 200)
    traffic.bind(finding_id, pair.id, TrafficRole.PROOF)

    traffic.assert_fresh((finding_id,), 1)
    with pytest.raises(TrafficError):
        traffic.assert_fresh((finding_id,), 0)


def test_traffic_from_another_operation_is_refused(stack, tmp_path: Path) -> None:
    traffic, kernel, _operation_id, finding_id = stack
    other = traffic.operations.create(
        "other", OperationMode.BUG_BOUNTY, ScopeContract(includes=("y",))
    )
    pair = traffic.record(other.id, "GET", "https://y", 200)

    with pytest.raises(TrafficError):
        traffic.bind(finding_id, pair.id, TrafficRole.PROOF)
