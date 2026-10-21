from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from samurai_ai.domain.kernel import ArtifactKind, FindingStatus
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.playbooks import (
    AssetKind,
    AssetScopeError,
    DuplicateCandidateError,
    PlaybookError,
    PlaybookModeError,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.playbooks import PlaybookService, ProgramRulesNotFoundError
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.playbooks import PlaybookRepository


def make_service(database_path: Path) -> PlaybookService:
    database = Database(database_path)
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    return PlaybookService(operations, kernel, PlaybookRepository(database))


def make_operation(service: PlaybookService) -> Operation:
    return service.operations.create(
        "Bounty lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",), excludes=("admin.example.com",)),
    )


def init(service: PlaybookService, operation_id: str) -> None:
    service.init_bug_bounty(
        operation_id,
        policy_url="https://policy.example.com",
        safe_harbor=True,
    )


def test_init_creates_rules_plan_and_rules_artifact(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    plan = service.init_bug_bounty(operation.id, policy_url="https://policy.example.com")

    assert [task.id for task in plan.tasks] == [
        "recon",
        "asset-inventory",
        "testing",
        "validation",
        "reporting",
    ]
    rules = service.get_rules(operation.id)
    assert rules.policy_url == "https://policy.example.com"
    assert service.kernel.kernel.artifact_exists(operation.id, "program-rules")

    event_types = [event.event_type for event in service.operations.events(operation.id)]
    assert "playbook.rules.set" in event_types
    assert "plan.created" in event_types


def test_init_requires_bug_bounty_mode(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = service.operations.create(
        "CTF", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    )
    with pytest.raises(PlaybookModeError):
        service.init_bug_bounty(operation.id)


def test_init_is_one_shot(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    init(service, operation.id)
    with pytest.raises(PlaybookError, match="one-shot"):
        init(service, operation.id)


def test_get_rules_requires_init(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    with pytest.raises(ProgramRulesNotFoundError):
        service.get_rules(operation.id)


def test_record_asset_enforces_scope(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    init(service, operation.id)

    entry = service.record_asset(operation.id, AssetKind.DOMAIN, "API.Example.COM.")
    assert entry.value == "api.example.com"

    with pytest.raises(AssetScopeError):
        service.record_asset(operation.id, AssetKind.DOMAIN, "admin.example.com")
    with pytest.raises(AssetScopeError):
        service.record_asset(operation.id, AssetKind.DOMAIN, "other.org")

    events = [event.event_type for event in service.operations.events(operation.id)]
    assert events.count("asset.rejected") == 2
    assert events.count("asset.recorded") == 1
    assert service.playbooks.list_assets(operation.id)[0].value == "api.example.com"


def test_record_asset_respects_allowed_kinds(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    service.init_bug_bounty(operation.id, asset_types=(AssetKind.DOMAIN,))
    with pytest.raises(PlaybookError, match="not allowed by program rules"):
        service.record_asset(operation.id, AssetKind.URL, "https://api.example.com")


def test_record_asset_is_idempotent(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    init(service, operation.id)
    first = service.record_asset(operation.id, AssetKind.DOMAIN, "api.example.com")
    second = service.record_asset(operation.id, AssetKind.DOMAIN, "API.example.COM")
    assert first.id == second.id
    events = [event.event_type for event in service.operations.events(operation.id)]
    assert events.count("asset.recorded") == 1


def test_service_asset_scope_uses_host(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    init(service, operation.id)
    entry = service.record_asset(operation.id, AssetKind.SERVICE, "API.example.com:8443")
    assert entry.value == "api.example.com:8443"
    with pytest.raises(AssetScopeError):
        service.record_asset(operation.id, AssetKind.SERVICE, "admin.example.com:443")


def test_asset_diff_with_retirement(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    init(service, operation.id)
    service.record_asset(operation.id, AssetKind.DOMAIN, "api.example.com")
    cutoff = datetime.now(UTC) - timedelta(seconds=1)
    service.record_asset(operation.id, AssetKind.DOMAIN, "www.example.com")
    service.retire_asset(operation.id, AssetKind.DOMAIN, "api.example.com")

    full = service.asset_diff(operation.id)
    assert {entry.value for entry in full.added} == {"www.example.com"}
    assert {entry.value for entry in full.removed} == {"api.example.com"}

    since = service.asset_diff(operation.id, since=cutoff)
    assert {entry.value for entry in since.unchanged} == set()
    assert {entry.value for entry in since.added} == {"www.example.com"}
    assert {entry.value for entry in since.removed} == {"api.example.com"}

    assert [entry.value for entry in service.list_assets(operation.id)] == ["www.example.com"]
    with pytest.raises(PlaybookError, match="already retired"):
        service.retire_asset(operation.id, AssetKind.DOMAIN, "api.example.com")


def test_propose_finding_dedup_flow(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    init(service, operation.id)
    artifact = service.kernel.register_artifact(
        operation.id, ArtifactKind.NOTE, "poc", requirement="finding-evidence"
    )

    finding = service.propose_finding(
        operation.id,
        "Reflected XSS",
        summary="poc",
        affected_asset="https://api.example.com/",
        category="web",
        evidence_ids=(artifact.id,),
    )
    assert finding.evidence_ids == (artifact.id,)

    with pytest.raises(DuplicateCandidateError) as caught:
        service.propose_finding(
            operation.id,
            "reflected   xss",
            affected_asset="https://api.example.com/",
            category="web",
        )
    assert caught.value.existing_finding_id == finding.id
    events = [event.event_type for event in service.operations.events(operation.id)]
    assert "finding.duplicate-candidate" in events

    forced = service.propose_finding(
        operation.id,
        "reflected xss",
        affected_asset="https://api.example.com/",
        category="web",
        force=True,
    )
    assert forced.id != finding.id

    service.kernel.mark_duplicate(forced.id, finding.id)
    assert (
        service.kernel.kernel.get_finding(forced.id).status is FindingStatus.DUPLICATE
    )


def test_propose_finding_rejects_out_of_scope_asset(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    init(service, operation.id)
    with pytest.raises(AssetScopeError):
        service.propose_finding(
            operation.id,
            "ssrf",
            affected_asset="https://other.org/",
            category="web",
        )


def test_summary_counts(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    init(service, operation.id)
    service.record_asset(operation.id, AssetKind.DOMAIN, "api.example.com")
    service.record_asset(operation.id, AssetKind.DOMAIN, "www.example.com")
    service.retire_asset(operation.id, AssetKind.DOMAIN, "www.example.com")
    finding = service.propose_finding(
        operation.id, "xss", affected_asset="api.example.com", category="web"
    )
    artifact = service.kernel.register_artifact(
        operation.id, ArtifactKind.NOTE, "poc", finding_id=finding.id
    )
    service.kernel.attach_evidence(finding.id, artifact.id)
    service.kernel.validate_finding(finding.id)

    summary = service.summary(operation.id)
    assert summary.assets_active == 1
    assert summary.assets_retired == 1
    assert summary.findings_total == 1
    assert summary.findings_validated == 1
