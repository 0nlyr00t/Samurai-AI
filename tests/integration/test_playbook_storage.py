import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.playbooks import (
    AssetKind,
    AssetLedgerEntry,
    BountyProgramRules,
    FindingFingerprintRecord,
)
from samurai_ai.domain.reports import BountyReport, ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.playbooks import PlaybookConflictError, PlaybookRepository
from samurai_ai.storage.reports import ReportRepository


def make_operation(database_path: Path) -> str:
    service = OperationService(OperationRepository(Database(database_path)))
    operation = service.create(
        "Bounty",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )
    return operation.id


def test_rules_survive_reopen_and_conflict_detection(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = PlaybookRepository(Database(database_path))
    rules = BountyProgramRules(operation_id=operation_id, policy_url="https://policy.example.com")
    event = OperationEvent(
        operation_id=operation_id, event_type="playbook.rules.set", payload={}
    )
    repository.save_rules(rules, None, event)

    reopened = PlaybookRepository(Database(database_path))
    restored = reopened.get_rules(operation_id)
    assert restored is not None
    assert restored.policy_url == "https://policy.example.com"

    with pytest.raises(PlaybookConflictError):
        reopened.save_rules(restored, expected_version=999, event=event)


def test_assets_are_immutable_and_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = PlaybookRepository(Database(database_path))
    entry = AssetLedgerEntry(
        operation_id=operation_id, kind=AssetKind.DOMAIN, value="api.example.com"
    )
    event = OperationEvent(operation_id=operation_id, event_type="asset.recorded", payload={})
    repository.add_asset(entry, event)

    with pytest.raises(sqlite3.IntegrityError):
        repository.add_asset(entry, event)

    with repository.database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE asset_ledger_entries SET value = 'evil.example.com' WHERE id = ?",
                (entry.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM asset_ledger_entries WHERE id = ?",
                (entry.id,),
            )


def test_retirements_and_fingerprints_roundtrip(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = PlaybookRepository(Database(database_path))
    entry = AssetLedgerEntry(
        operation_id=operation_id, kind=AssetKind.URL, value="https://api.example.com/"
    )
    repository.add_asset(
        entry, OperationEvent(operation_id=operation_id, event_type="asset.recorded", payload={})
    )
    repository.retire_asset(
        operation_id,
        AssetKind.URL,
        entry.value,
        OperationEvent(operation_id=operation_id, event_type="asset.retired", payload={}),
    )
    retirements = repository.list_retirements(operation_id)
    assert retirements == [
        (AssetKind.URL, "https://api.example.com/", retirements[0][2])
    ]

    from samurai_ai.domain.kernel import Finding
    from samurai_ai.storage.kernel import KernelRepository

    kernel = KernelRepository(Database(database_path))
    finding = Finding(id="find_1", operation_id=operation_id, title="xss")
    kernel.add_finding(
        finding,
        OperationEvent(operation_id=operation_id, event_type="finding.created", payload={}),
    )
    record = FindingFingerprintRecord(
        finding_id="find_1",
        operation_id=operation_id,
        fingerprint="ab" * 32,
        asset="api.example.com",
        category="web",
    )
    repository.add_fingerprint(record)
    found = repository.find_fingerprint(operation_id, "ab" * 32)
    assert found is not None
    assert found.finding_id == "find_1"
    assert repository.find_fingerprint(operation_id, "cd" * 32) is None


def test_reports_are_immutable(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = ReportRepository(Database(database_path))
    report = BountyReport(
        operation_id=operation_id,
        format=ReportFormat.MARKDOWN,
        sha256="ab" * 32,
        size_bytes=5,
        path="reports/x/rep_1.md",
        finding_ids=("find_1",),
        asset_count=1,
    )
    repository.add_report(
        report,
        OperationEvent(operation_id=operation_id, event_type="report.generated", payload={}),
    )

    reopened = ReportRepository(Database(database_path))
    assert reopened.list_reports(operation_id)[0].sha256 == "ab" * 32
    with reopened.database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE reports SET sha256 = 'tampered' WHERE id = ?",
                (report.id,),
            )
