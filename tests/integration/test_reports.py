import json
from pathlib import Path

import pytest

from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity, FindingStatus
from samurai_ai.domain.operations import (
    Operation,
    OperationEvent,
    OperationMode,
    OperationStatus,
)
from samurai_ai.domain.playbooks import AssetKind, AssetLedgerEntry
from samurai_ai.domain.reports import (
    ReportFormat,
    ReportNotFoundError,
    ReportScopeViolationError,
    ReportStateError,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.reports import ReportService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.playbooks import PlaybookRepository
from samurai_ai.storage.reports import ReportRepository
from samurai_ai.storage.vault import VaultRepository


class Stack:
    def __init__(self, database_path: Path, with_vault: bool = False) -> None:
        database = Database(database_path)
        self.operations = OperationService(OperationRepository(database))
        self.kernel = KernelService(self.operations, KernelRepository(database))
        self.playbooks = PlaybookService(
            self.operations, self.kernel, PlaybookRepository(database)
        )
        vault = None
        if with_vault:
            vault = VaultService(
                VaultRepository(database),
                self.operations.repository,
                database_path.parent / "vault.key",
            )
        self.vault = vault
        self.reports = ReportService(
            self.operations,
            self.kernel,
            self.playbooks,
            ReportRepository(database),
            vault,
        )


def make_operation(stack: Stack) -> Operation:
    operation = stack.operations.create(
        "Bounty lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )
    stack.playbooks.init_bug_bounty(operation.id)
    return operation


def add_validated_finding(
    stack: Stack, operation_id: str, title: str = "Reflected XSS"
) -> str:
    finding = stack.kernel.create_finding(
        operation_id, title, FindingSeverity.HIGH, summary="poc summary"
    )
    artifact = stack.kernel.register_artifact(
        operation_id, ArtifactKind.NOTE, "proof of concept", summary="poc"
    )
    stack.kernel.attach_evidence(finding.id, artifact.id)
    stack.kernel.validate_finding(finding.id)
    return finding.id


def test_generate_requires_verification_or_reporting(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    add_validated_finding(stack, operation.id)
    with pytest.raises(ReportStateError, match="verification or reporting"):
        stack.reports.generate(operation.id)


def test_generate_requires_validated_findings(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation.id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation.id)
    stack.kernel.transition_operation(operation.id, OperationStatus.EXECUTION)
    stack.kernel.transition_operation(operation.id, OperationStatus.VERIFICATION)
    with pytest.raises(ReportStateError, match="no validated findings"):
        stack.reports.generate(operation.id)


def drive_to_verification(stack: Stack, operation_id: str) -> None:
    stack.kernel.transition_operation(operation_id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation_id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.EXECUTION)
    stack.kernel.transition_operation(operation_id, OperationStatus.VERIFICATION)


def test_generate_markdown_only_validated_and_registers_artifact(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    validated_id = add_validated_finding(stack, operation.id)
    candidate = stack.kernel.create_finding(operation.id, "Unverified hunch")
    stack.playbooks.record_asset(operation.id, AssetKind.DOMAIN, "api.example.com")
    drive_to_verification(stack, operation.id)

    report = stack.reports.generate(operation.id)
    text = (tmp_path / report.path).read_text(encoding="utf-8")

    assert validated_id in text
    assert candidate.title not in text
    assert "api.example.com" in text
    assert stack.kernel.kernel.artifact_exists(operation.id, "final-report")
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "report.generated" in events


def test_generate_json_format(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    finding_id = add_validated_finding(stack, operation.id)
    drive_to_verification(stack, operation.id)

    report = stack.reports.generate(operation.id, ReportFormat.JSON)
    payload = json.loads((tmp_path / report.path).read_text(encoding="utf-8"))
    assert payload["findings"][0]["id"] == finding_id
    assert payload["findings"][0]["evidence"][0]["sha256"]


def test_report_rejects_out_of_scope_reference(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    add_validated_finding(stack, operation.id)
    # simulate a ledger entry that fell out of scope after a scope change
    stack.playbooks.playbooks.add_asset(
        AssetLedgerEntry(
            operation_id=operation.id,
            kind=AssetKind.DOMAIN,
            value="evil.other.org",
        ),
        OperationEvent(operation_id=operation.id, event_type="asset.recorded", payload={}),
    )
    drive_to_verification(stack, operation.id)

    with pytest.raises(ReportScopeViolationError, match="evil.other.org"):
        stack.reports.generate(operation.id)
    assert stack.reports.reports.list_reports(operation.id) == []
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "report.rejected" in events


def test_report_redacts_vault_secrets(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db", with_vault=True)
    operation = make_operation(stack)
    assert stack.vault is not None
    stack.vault.add("api-token", "sk-live-secret-value-123", operation.id)
    finding = stack.kernel.create_finding(
        operation.id,
        "Auth bypass",
        FindingSeverity.CRITICAL,
        summary="leaked sk-live-secret-value-123 in response",
    )
    artifact = stack.kernel.register_artifact(
        operation.id, ArtifactKind.NOTE, "token sk-live-secret-value-123 observed"
    )
    stack.kernel.attach_evidence(finding.id, artifact.id)
    stack.kernel.validate_finding(finding.id)
    drive_to_verification(stack, operation.id)

    report = stack.reports.generate(operation.id)
    text = (tmp_path / report.path).read_text(encoding="utf-8")
    assert "sk-live-secret-value-123" not in text
    assert "***REDACTED***" in text


def test_export_builds_manifest_with_hashes(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    finding_id = add_validated_finding(stack, operation.id)
    drive_to_verification(stack, operation.id)
    report = stack.reports.generate(operation.id)

    dest = tmp_path / "export"
    manifest = stack.reports.export(operation.id, report.id, dest)

    assert (dest / "report.md").exists()
    assert (dest / "manifest.json").exists()
    artifact_files = list((dest / "artifacts").iterdir())
    assert len(artifact_files) == 1
    paths = {entry.path for entry in manifest.files}
    assert "report.md" in paths
    assert any(path.startswith("artifacts/") for path in paths)
    assert manifest.finding_ids == (finding_id,)
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "report.exported" in events

    payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert payload["report_id"] == report.id


def test_export_unknown_report(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    with pytest.raises(ReportNotFoundError):
        stack.reports.export(operation.id, "rep_missing", tmp_path / "export")


def test_mark_reported_flow(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    finding_id = add_validated_finding(stack, operation.id)
    drive_to_verification(stack, operation.id)
    report = stack.reports.generate(operation.id)
    stack.reports.export(operation.id, report.id, tmp_path / "export")

    updated = stack.reports.mark_reported(operation.id, [finding_id])
    assert updated[0].status is FindingStatus.REPORTED


def test_a_report_shows_the_graded_impact(tmp_path: Path) -> None:
    from samurai_ai.domain.impact import ImpactLevel
    from samurai_ai.services.impact import ImpactService

    stack = Stack(tmp_path / "impact.db")
    impact = ImpactService(stack.operations, stack.kernel)
    stack.reports.impact = impact
    operation = make_operation(stack)
    finding_id = add_validated_finding(stack, operation.id, "IDOR on orders")
    artifact = stack.kernel.get_finding_by_id(finding_id).evidence_ids[0]
    drive_to_verification(stack, operation.id)
    impact.record(finding_id, ImpactLevel.AUTH_BYPASS, (artifact,), rollback="undo")

    report = stack.reports.generate(operation.id)
    text = (tmp_path / report.path).read_text(encoding="utf-8")
    assert "impact: auth-bypass (3 points, reportable)" in text


def test_a_threshold_refuses_a_low_impact_report(tmp_path: Path) -> None:
    from samurai_ai.domain.impact import ImpactLevel
    from samurai_ai.services.impact import ImpactService

    stack = Stack(tmp_path / "impact-threshold.db")
    impact = ImpactService(stack.operations, stack.kernel)
    stack.reports.impact = impact
    operation = make_operation(stack)
    finding_id = add_validated_finding(stack, operation.id)
    artifact = stack.kernel.get_finding_by_id(finding_id).evidence_ids[0]
    drive_to_verification(stack, operation.id)
    impact.record(finding_id, ImpactLevel.READ, (artifact,))

    with pytest.raises(ReportStateError, match="impact below 3 points"):
        stack.reports.generate(operation.id, min_impact_points=3)

    impact.record(finding_id, ImpactLevel.AUTH_BYPASS, (artifact,), rollback="undo")
    assert stack.reports.generate(operation.id, min_impact_points=3) is not None
