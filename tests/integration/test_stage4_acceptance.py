"""Stage 4 acceptance criteria as executable tests (Bug Bounty playbook).

1. A full bug bounty operator path works end to end: program rules, plan
   template, scope-enforced asset ledger with diff, finding dedup, governed
   gates, report generation and evidence package export.
2. Reports contain only validated findings and never leak secrets or
   out-of-scope data.
3. The whole state survives a database reopen.
"""

import hashlib
import json
from pathlib import Path

import pytest

from samurai_ai.domain.kernel import ArtifactKind, FindingStatus
from samurai_ai.domain.operations import (
    OperationEvent,
    OperationMode,
    OperationStatus,
)
from samurai_ai.domain.playbooks import (
    AssetKind,
    AssetLedgerEntry,
    AssetScopeError,
    DuplicateCandidateError,
)
from samurai_ai.domain.reports import ReportScopeViolationError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelPreconditionError, KernelService
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
    def __init__(self, database_path: Path) -> None:
        database = Database(database_path)
        self.operations = OperationService(OperationRepository(database))
        self.kernel = KernelService(self.operations, KernelRepository(database))
        self.playbooks = PlaybookService(
            self.operations, self.kernel, PlaybookRepository(database)
        )
        self.vault = VaultService(
            VaultRepository(database),
            self.operations.repository,
            database_path.parent / "vault.key",
        )
        self.reports = ReportService(
            self.operations,
            self.kernel,
            self.playbooks,
            ReportRepository(database),
            self.vault,
        )


def test_stage4_bug_bounty_acceptance(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    stack = Stack(database_path)
    operation = stack.operations.create(
        "Acceptance bounty",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",), excludes=("admin.example.com",)),
        objective="Find and report validated vulnerabilities",
    )
    operation_id = operation.id

    # --- playbook init: rules, template plan, program-rules artifact ---
    plan = stack.playbooks.init_bug_bounty(
        operation_id, policy_url="https://policy.example.com", safe_harbor=True
    )
    assert [task.id for task in plan.tasks] == [
        "recon",
        "asset-inventory",
        "testing",
        "validation",
        "reporting",
    ]

    # --- scope-enforced asset ledger ---
    stack.playbooks.record_asset(operation_id, AssetKind.DOMAIN, "API.Example.COM.")
    stack.playbooks.record_asset(
        operation_id, AssetKind.URL, "https://api.example.com/login"
    )
    with pytest.raises(AssetScopeError):
        stack.playbooks.record_asset(operation_id, AssetKind.DOMAIN, "admin.example.com")
    with pytest.raises(AssetScopeError):
        stack.playbooks.record_asset(operation_id, AssetKind.DOMAIN, "other.org")
    diff = stack.playbooks.asset_diff(operation_id)
    assert {entry.value for entry in diff.added} == {
        "api.example.com",
        "https://api.example.com/login",
    }

    # --- lifecycle to execution ---
    stack.kernel.transition_operation(operation_id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation_id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.EXECUTION)

    # --- tasks with governed artifact requirements ---
    stack.kernel.start_task(operation_id, "recon")
    stack.kernel.complete_task(operation_id, "recon")
    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.NOTE,
        "subdomains enumerated",
        requirement="recon-notes",
        task_id="asset-inventory",
        gate_id="recon-complete",
    )
    stack.kernel.start_task(operation_id, "asset-inventory")
    stack.kernel.complete_task(operation_id, "asset-inventory")
    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.LOG,
        "asset ledger snapshot",
        requirement="asset-ledger",
        task_id="testing",
    )
    stack.kernel.start_task(operation_id, "testing")

    # --- finding with evidence, dedup and a secret that must never leak ---
    stack.vault.add("api-key", "bb-secret-token-xyz", operation_id)
    evidence = stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.NOTE,
        "poc for xss",
        requirement="finding-evidence",
        task_id="validation",
    )
    finding = stack.playbooks.propose_finding(
        operation_id,
        "Reflected XSS",
        summary="token bb-secret-token-xyz leaked in response",
        remediation="escape output",
        affected_asset="https://api.example.com/login",
        category="web",
        evidence_ids=(evidence.id,),
    )
    with pytest.raises(DuplicateCandidateError):
        stack.playbooks.propose_finding(
            operation_id,
            "reflected   XSS",
            affected_asset="https://api.example.com/login",
            category="web",
        )
    forced = stack.playbooks.propose_finding(
        operation_id,
        "reflected xss",
        affected_asset="https://api.example.com/login",
        category="web",
        force=True,
    )
    stack.kernel.mark_duplicate(forced.id, finding.id)
    stack.kernel.validate_finding(finding.id)

    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.LOG,
        "testing completed",
        requirement="testing-log",
        gate_id="testing-complete",
    )
    stack.kernel.complete_task(operation_id, "testing")
    stack.kernel.start_task(operation_id, "validation")
    stack.kernel.complete_task(operation_id, "validation")

    # --- verification: report generation is possible, gates block reporting ---
    stack.kernel.transition_operation(operation_id, OperationStatus.VERIFICATION)
    with pytest.raises(KernelPreconditionError, match="gates not passed"):
        stack.kernel.transition_operation(operation_id, OperationStatus.REPORTING)

    report = stack.reports.generate(operation_id)
    report_text = (tmp_path / report.path).read_text(encoding="utf-8")
    # only validated findings, redacted secrets
    assert "Reflected XSS" in report_text
    assert "bb-secret-token-xyz" not in report_text
    assert report.finding_ids == (finding.id,)

    stack.kernel.start_task(operation_id, "reporting")
    stack.kernel.complete_task(operation_id, "reporting")
    for gate_id in ("scope-locked", "recon-complete", "testing-complete", "ready-to-report"):
        stack.kernel.pass_gate(operation_id, gate_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.REPORTING)

    # --- evidence package export ---
    dest = tmp_path / "package"
    manifest = stack.reports.export(operation_id, report.id, dest)
    assert (dest / "report.md").exists()
    manifest_payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest_payload["files"]:
        data = (dest / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert manifest.finding_ids == (finding.id,)

    stack.reports.mark_reported(operation_id, [finding.id])
    stack.kernel.transition_operation(operation_id, OperationStatus.COMPLETE)

    # --- journal carries the full governed trail ---
    event_types = [event.event_type for event in stack.operations.events(operation_id)]
    for expected in (
        "playbook.rules.set",
        "asset.recorded",
        "asset.rejected",
        "asset.diff.computed",
        "finding.duplicate-candidate",
        "finding.validated",
        "finding.marked-duplicate",
        "report.generated",
        "report.exported",
        "finding.reported",
    ):
        assert expected in event_types, expected

    # --- everything survives a reopen ---
    reopened = Stack(database_path)
    restored = reopened.operations.get(operation_id)
    assert restored.status is OperationStatus.COMPLETE
    reopened_report = reopened.reports.reports.get_report(report.id)
    assert reopened_report is not None
    assert reopened_report.sha256 == report.sha256
    assert (
        reopened.kernel.kernel.get_finding(finding.id).status is FindingStatus.REPORTED
    )
    assert {entry.value for entry in reopened.playbooks.list_assets(operation_id)} == {
        "api.example.com",
        "https://api.example.com/login",
    }


def test_stage4_report_blocks_out_of_scope_data(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = stack.operations.create(
        "Scope guard",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )
    stack.playbooks.init_bug_bounty(operation.id)
    finding = stack.kernel.create_finding(operation.id, "Valid finding")
    evidence = stack.kernel.register_artifact(operation.id, ArtifactKind.NOTE, "poc")
    stack.kernel.attach_evidence(finding.id, evidence.id)
    stack.kernel.validate_finding(finding.id)
    stack.kernel.transition_operation(operation.id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation.id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation.id)
    stack.kernel.transition_operation(operation.id, OperationStatus.EXECUTION)
    stack.kernel.transition_operation(operation.id, OperationStatus.VERIFICATION)

    # ledger entry inserted directly, bypassing the scope-enforcing service,
    # simulates data that fell out of scope after the fact
    stack.playbooks.playbooks.add_asset(
        AssetLedgerEntry(
            operation_id=operation.id, kind=AssetKind.DOMAIN, value="rogue.other.org"
        ),
        OperationEvent(operation_id=operation.id, event_type="asset.recorded", payload={}),
    )
    with pytest.raises(ReportScopeViolationError):
        stack.reports.generate(operation.id)
    assert stack.reports.reports.list_reports(operation.id) == []
