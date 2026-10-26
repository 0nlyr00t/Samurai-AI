from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from samurai_ai.domain.kernel import (
    ArtifactKind,
    Finding,
    FindingSeverity,
    FindingStatus,
    GateStatus,
    OperationPlan,
    TaskStatus,
)
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.playbooks import (
    AssetDiff,
    AssetKind,
    AssetLedgerEntry,
    AssetScopeError,
    BountyProgramRules,
    DuplicateCandidateError,
    FindingFingerprintRecord,
    PlaybookError,
    PlaybookModeError,
    PlaybookTemplate,
    bug_bounty_template,
    finding_fingerprint,
    normalize_asset_value,
    scope_target_for_asset,
)
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.playbooks import PlaybookRepository


class ProgramRulesNotFoundError(LookupError):
    pass


class PlaybookSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    assets_active: int
    assets_retired: int
    findings_total: int
    findings_validated: int
    findings_duplicate: int
    findings_reported: int


class PlaybookService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        playbooks: PlaybookRepository,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.playbooks = playbooks

    def init_bug_bounty(
        self,
        operation_id: str,
        policy_url: str = "",
        asset_types: tuple[AssetKind, ...] = tuple(AssetKind),
        safe_harbor: bool = False,
        notes: str = "",
    ) -> OperationPlan:
        operation = self.operations.get(operation_id)
        if operation.mode is not OperationMode.BUG_BOUNTY:
            raise PlaybookModeError(
                f"operation {operation_id} is in {operation.mode.value} mode, "
                "not bug-bounty"
            )
        if self.playbooks.get_rules(operation_id) is not None:
            raise PlaybookError(
                f"operation {operation_id} already has program rules; "
                "playbook init is one-shot"
            )
        rules = BountyProgramRules(
            operation_id=operation_id,
            policy_url=policy_url,
            asset_types=asset_types,
            safe_harbor=safe_harbor,
            notes=notes,
        )
        rules_event = OperationEvent(
            operation_id=operation_id,
            event_type="playbook.rules.set",
            payload={
                "playbook": "bug-bounty",
                "asset_types": [kind.value for kind in rules.asset_types],
                "safe_harbor": rules.safe_harbor,
            },
        )
        self.playbooks.save_rules(rules, None, rules_event)
        template = bug_bounty_template()
        plan = self.kernel.create_plan(
            operation_id, template.title, template.tasks, template.gates
        )
        rules_gate = next(
            (
                gate
                for gate in template.gates
                if "program-rules" in gate.required_artifacts
            ),
            None,
        )
        self.kernel.register_artifact(
            operation_id,
            ArtifactKind.NOTE,
            rules.describe(),
            summary="Bug bounty program rules",
            requirement="program-rules",
            gate_id=rules_gate.id if rules_gate is not None else None,
            created_by="operator",
        )
        return plan

    def install_custom(self, path: Path, created_by: str = "operator") -> PlaybookTemplate:
        raw = path.read_text(encoding="utf-8")
        template = PlaybookTemplate.model_validate_json(raw)
        # structural validation (unique ids, known dependencies, no cycles)
        # lives in OperationPlan — reuse it before anything is stored
        OperationPlan(
            operation_id="install-validation",
            title=template.title,
            tasks=template.tasks,
            gates=template.gates,
        )
        # custom templates always start from a clean state, whatever the file says
        for task in template.tasks:
            task.status = TaskStatus.PENDING
        for gate in template.gates:
            gate.status = GateStatus.PENDING
        if self.playbooks.get_custom_playbook(template.name) is not None:
            raise PlaybookError(
                f"custom playbook {template.name!r} is already installed"
            )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self.playbooks.add_custom_playbook(template, digest, created_by)
        return template

    def use_custom(self, operation_id: str, name: str) -> OperationPlan:
        template = self.playbooks.get_custom_playbook(name)
        if template is None:
            raise PlaybookError(f"custom playbook not found: {name}")
        self.operations.get(operation_id)
        return self.kernel.create_plan(
            operation_id, template.title, template.tasks, template.gates
        )

    def list_custom(self) -> list[PlaybookTemplate]:
        return self.playbooks.list_custom_playbooks()

    def get_rules(self, operation_id: str) -> BountyProgramRules:
        self.operations.get(operation_id)
        rules = self.playbooks.get_rules(operation_id)
        if rules is None:
            raise ProgramRulesNotFoundError(
                f"operation {operation_id} has no program rules; run playbook init first"
            )
        return rules

    def record_asset(
        self,
        operation_id: str,
        kind: AssetKind,
        value: str,
        source: str = "operator",
    ) -> AssetLedgerEntry:
        self.operations.get(operation_id)
        rules = self.get_rules(operation_id)
        if kind not in rules.asset_types:
            raise PlaybookError(
                f"asset kind {kind.value} is not allowed by program rules "
                f"(allowed: {', '.join(item.value for item in rules.asset_types)})"
            )
        normalized = normalize_asset_value(kind, value)
        decision = self.operations.check_scope(
            operation_id, scope_target_for_asset(kind, normalized)
        )
        if not decision.allowed:
            event = OperationEvent(
                operation_id=operation_id,
                event_type="asset.rejected",
                payload={
                    "kind": kind.value,
                    "value": normalized,
                    "reason": decision.reason,
                },
            )
            self.operations.repository.append_event(event)
            raise AssetScopeError(
                f"asset {normalized} rejected by scope: {decision.reason}"
            )
        existing = self.playbooks.get_asset(operation_id, kind, normalized)
        if existing is not None:
            return existing
        entry = AssetLedgerEntry(
            operation_id=operation_id,
            kind=kind,
            value=normalized,
            source=source,
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="asset.recorded",
            payload={"asset_id": entry.id, "kind": kind.value, "value": normalized},
        )
        try:
            self.playbooks.add_asset(entry, event)
        except sqlite3.IntegrityError:
            return self.playbooks.get_asset(operation_id, kind, normalized) or entry
        return entry

    def retire_asset(self, operation_id: str, kind: AssetKind, value: str) -> None:
        self.operations.get(operation_id)
        normalized = normalize_asset_value(kind, value)
        entry = self.playbooks.get_asset(operation_id, kind, normalized)
        if entry is None:
            raise PlaybookError(f"asset not in ledger: {kind.value} {normalized}")
        retired = {
            (item_kind, item_value)
            for item_kind, item_value, _ in self.playbooks.list_retirements(operation_id)
        }
        if (kind, normalized) in retired:
            raise PlaybookError(f"asset already retired: {kind.value} {normalized}")
        event = OperationEvent(
            operation_id=operation_id,
            event_type="asset.retired",
            payload={"kind": kind.value, "value": normalized},
        )
        self.playbooks.retire_asset(operation_id, kind, normalized, event)

    def list_assets(self, operation_id: str, active_only: bool = True) -> list[AssetLedgerEntry]:
        self.operations.get(operation_id)
        entries = self.playbooks.list_assets(operation_id)
        if not active_only:
            return entries
        retired = {
            (item_kind, item_value)
            for item_kind, item_value, _ in self.playbooks.list_retirements(operation_id)
        }
        return [
            entry for entry in entries if (entry.kind, entry.value) not in retired
        ]

    def asset_diff(self, operation_id: str, since: datetime | None = None) -> AssetDiff:
        self.operations.get(operation_id)
        entries = self.playbooks.list_assets(operation_id)
        retirements = self.playbooks.list_retirements(operation_id)
        retired_keys = {(kind, value) for kind, value, _ in retirements}
        added: list[AssetLedgerEntry] = []
        unchanged: list[AssetLedgerEntry] = []
        for entry in entries:
            if (entry.kind, entry.value) in retired_keys:
                continue
            if since is not None and entry.created_at > since:
                added.append(entry)
            elif since is None:
                added.append(entry)
            else:
                unchanged.append(entry)
        removed: list[AssetLedgerEntry] = []
        retired_after = {
            (kind, value) for kind, value, when in retirements if since is None or when > since
        }
        for entry in entries:
            if (entry.kind, entry.value) in retired_after:
                removed.append(entry)
        diff = AssetDiff(
            operation_id=operation_id,
            added=tuple(added),
            removed=tuple(removed),
            unchanged=tuple(unchanged),
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="asset.diff.computed",
            payload={
                "added": len(added),
                "removed": len(removed),
                "unchanged": len(unchanged),
            },
        )
        self.operations.repository.append_event(event)
        return diff

    def propose_finding(
        self,
        operation_id: str,
        title: str,
        severity: FindingSeverity = FindingSeverity.INFO,
        summary: str = "",
        remediation: str = "",
        affected_asset: str = "",
        category: str = "general",
        evidence_ids: tuple[str, ...] = (),
        force: bool = False,
    ) -> Finding:
        self.operations.get(operation_id)
        asset = affected_asset.strip()
        if not asset:
            raise PlaybookError("affected asset is required for playbook findings")
        decision = self.operations.check_scope(operation_id, asset)
        if not decision.allowed:
            raise AssetScopeError(f"affected asset {asset} is out of scope: {decision.reason}")
        fingerprint = finding_fingerprint(title, asset, category)
        existing = self.playbooks.find_fingerprint(operation_id, fingerprint)
        if existing is not None and not force:
            event = OperationEvent(
                operation_id=operation_id,
                event_type="finding.duplicate-candidate",
                payload={
                    "fingerprint": fingerprint,
                    "existing_finding_id": existing.finding_id,
                    "title": title,
                },
            )
            self.operations.repository.append_event(event)
            raise DuplicateCandidateError(fingerprint, existing.finding_id)
        finding = self.kernel.create_finding(
            operation_id,
            title,
            severity,
            summary=summary,
            remediation=remediation,
        )
        for artifact_id in evidence_ids:
            finding = self.kernel.attach_evidence(finding.id, artifact_id)
        self.playbooks.add_fingerprint(
            FindingFingerprintRecord(
                finding_id=finding.id,
                operation_id=operation_id,
                fingerprint=fingerprint,
                asset=asset,
                category=category,
            )
        )
        return finding

    def summary(self, operation_id: str) -> PlaybookSummary:
        self.operations.get(operation_id)
        active = self.list_assets(operation_id)
        retired = self.playbooks.list_retirements(operation_id)
        findings = self.kernel.kernel.list_findings(operation_id)
        return PlaybookSummary(
            operation_id=operation_id,
            assets_active=len(active),
            assets_retired=len(retired),
            findings_total=len(findings),
            findings_validated=sum(
                1 for finding in findings if finding.status is FindingStatus.VALIDATED
            ),
            findings_duplicate=sum(
                1 for finding in findings if finding.status is FindingStatus.DUPLICATE
            ),
            findings_reported=sum(
                1 for finding in findings if finding.status is FindingStatus.REPORTED
            ),
        )
