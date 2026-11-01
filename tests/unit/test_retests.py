"""Retests: the verdict contract, especially what `fixed` costs."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity, FindingStatus, PlanTask
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.retest import RetestError, RetestVerdict
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.retests import RetestService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.retests import RetestRepository


@pytest.fixture
def stack(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "retests.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    retests = RetestService(operations, kernel, RetestRepository(database))
    operation = operations.create("gate", OperationMode.BUG_BOUNTY, ScopeContract(includes=("x",)))
    kernel.create_plan(operation.id, "plan", [PlanTask(id="recon", title="Recon")], [])
    artifact = kernel.register_artifact(operation.id, ArtifactKind.COMMAND_OUTPUT, "body")
    finding = kernel.create_finding(operation.id, "IDOR", FindingSeverity.HIGH, "cross-tenant")
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.validate_finding(finding.id)
    return retests, kernel, operation.id, finding.id


def test_a_retest_starts_for_a_validated_finding(stack) -> None:
    retests, _kernel, operation_id, finding_id = stack

    retest = retests.start(finding_id)

    assert retest.open
    assert retest.operation_id == operation_id
    assert "retest.started" in [
        event.event_type for event in retests.operations.repository.events(operation_id)
    ]


def test_a_candidate_finding_is_not_retestable(stack) -> None:
    retests, kernel, operation_id, _finding = stack
    other = kernel.create_finding(operation_id, "raw", FindingSeverity.LOW)

    with pytest.raises(RetestError):
        retests.start(other.id)


def test_only_one_open_retest_per_finding(stack) -> None:
    retests, _kernel, _operation_id, finding_id = stack
    retests.start(finding_id)

    with pytest.raises(RetestError):
        retests.start(finding_id)


def test_reproduced_needs_evidence(stack) -> None:
    retests, _kernel, _operation_id, finding_id = stack
    retest = retests.start(finding_id)

    with pytest.raises(RetestError):
        retests.finish(retest.id, RetestVerdict.REPRODUCED, "still reads other user", "")


def test_fixed_needs_a_control(stack) -> None:
    retests, _kernel, _operation_id, finding_id = stack
    retest = retests.start(finding_id)

    with pytest.raises(RetestError):
        retests.finish(
            retest.id, RetestVerdict.FIXED, "403 now", "GET /user/2 -> 403", ""
        )


def test_fixed_flips_the_finding_status(stack) -> None:
    retests, kernel, _operation_id, finding_id = stack
    retest = retests.start(finding_id)

    finished = retests.finish(
        retest.id,
        RetestVerdict.FIXED,
        "403 on the other user's object",
        "GET /user/2 -> 403",
        "GET /user/1 as user 1 -> 200",
    )

    assert finished.verdict is RetestVerdict.FIXED
    assert kernel.kernel.get_finding(finding_id).status is FindingStatus.FIXED


def test_reproduced_keeps_the_status(stack) -> None:
    retests, kernel, _operation_id, finding_id = stack
    retest = retests.start(finding_id)

    retests.finish(
        retest.id, RetestVerdict.REPRODUCED, "still leaks", "GET /user/2 -> 200 with email"
    )

    assert kernel.kernel.get_finding(finding_id).status is FindingStatus.VALIDATED


def test_inconclusive_records_what_blocked_it(stack) -> None:
    retests, _kernel, _operation_id, finding_id = stack
    retest = retests.start(finding_id)

    finished = retests.finish(
        retest.id,
        RetestVerdict.INCONCLUSIVE,
        "target behind a WAF captcha",
        "curl -> 200 with captcha page",
    )

    assert finished.verdict is RetestVerdict.INCONCLUSIVE


def test_a_finished_retest_cannot_be_finished_again(stack) -> None:
    retests, _kernel, _operation_id, finding_id = stack
    retest = retests.start(finding_id)
    retests.finish(retest.id, RetestVerdict.REPRODUCED, "still leaks", "proof")

    with pytest.raises(RetestError):
        retests.finish(retest.id, RetestVerdict.FIXED, "x", "y", "z")


def test_verdicts_are_journalled(stack) -> None:
    retests, _kernel, operation_id, finding_id = stack
    retest = retests.start(finding_id)
    retests.finish(retest.id, RetestVerdict.REPRODUCED, "still leaks", "proof")

    types = [event.event_type for event in retests.operations.repository.events(operation_id)]
    assert "retest.finished" in types


def test_history_is_kept_per_finding(stack) -> None:
    retests, _kernel, _operation_id, finding_id = stack
    first = retests.start(finding_id)
    retests.finish(first.id, RetestVerdict.INCONCLUSIVE, "blocked", "captcha")

    second = retests.start(finding_id)
    retests.finish(second.id, RetestVerdict.REPRODUCED, "now reproducible", "200 leak")

    assert [retest.verdict for retest in retests.for_finding(finding_id)] == [
        RetestVerdict.INCONCLUSIVE,
        RetestVerdict.REPRODUCED,
    ]
