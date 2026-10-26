"""Integration export tests: CALDERA pack, VECTR campaign, HTML dossier/report."""

import json
from pathlib import Path

import pytest

from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity
from samurai_ai.domain.operations import OperationMode, OperationStatus
from samurai_ai.domain.planning import KillChainPhase
from samurai_ai.domain.reports import ReportError, ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.exports import IntegrationExportService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.planning import PlanningService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.reports import ReportService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.planning import PlanningRepository
from samurai_ai.storage.playbooks import PlaybookRepository
from samurai_ai.storage.redteam import RedTeamRepository
from samurai_ai.storage.reports import ReportRepository
from samurai_ai.storage.vault import VaultRepository

CANARY = "CANARY-SECRET-VALUE"


class Stack:
    def __init__(self, database_path: Path) -> None:
        database = Database(database_path)
        self.database = database
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
        self.redteam = RedTeamService(
            self.operations, self.kernel, RedTeamRepository(database), self.vault
        )
        self.planning = PlanningService(
            self.operations, self.kernel, PlanningRepository(database), self.vault
        )
        self.reports = ReportService(
            self.operations,
            self.kernel,
            self.playbooks,
            ReportRepository(database),
            self.vault,
        )
        self.exports = IntegrationExportService(
            self.operations, self.kernel, self.redteam, self.planning, self.vault
        )


def test_caldera_export_pack_structure_and_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    stack = Stack(tmp_path / "caldera.db")
    operation = stack.operations.create(
        "RT lab", OperationMode.RED_TEAM, ScopeContract(includes=("example.com",))
    )
    stack.vault.add("rt-token", CANARY, operation.id)
    executed_with_tactic = stack.redteam.add_step(
        operation.id, "Phish entry", "T1566", tactic="initial-access",
        notes=f"used token {CANARY}",
    )
    executed_default = stack.redteam.add_step(
        operation.id, "Run implant", "T1059",
    )
    planned = stack.redteam.add_step(operation.id, "Exfiltrate", "T1041")
    for step in (executed_with_tactic, executed_default):
        stack.redteam.update_step(step.id, "start")
        stack.redteam.update_step(step.id, "complete", notes="done")

    result = stack.exports.export_caldera(operation.id, tmp_path / "pack")

    profile = (tmp_path / "pack" / "adversaries" / f"samurai-{operation.id}.yml").read_text(
        encoding="utf-8"
    )
    assert "atomic_ordering:" in profile
    assert f"samurai-{executed_with_tactic.id}" in profile
    assert f"samurai-{executed_default.id}" in profile
    assert f"samurai-{planned.id}" not in profile  # only executed steps exported

    ability_access = (
        tmp_path / "pack" / "abilities" / "initial-access"
        / f"samurai-{executed_with_tactic.id}.yml"
    ).read_text(encoding="utf-8")
    assert 'tactic: "initial-access"' in ability_access
    assert 'technique_id: "T1566"' in ability_access

    ability_default = (
        tmp_path / "pack" / "abilities" / "execution"
        / f"samurai-{executed_default.id}.yml"
    ).read_text(encoding="utf-8")
    assert 'tactic: "execution"' in ability_default
    assert "tactic defaulted" in ability_default

    # секрет redacted во всех файлах
    assert CANARY not in ability_access
    for file in result["files"]:
        text = Path(file["path"]).read_text(encoding="utf-8")
        assert CANARY not in text

    # governed artifact + событие
    events = stack.operations.events(operation.id)
    assert any(
        e.event_type == "export.generated"
        and e.payload["format"] == "caldera-adversary-pack"
        for e in events
    )


def test_caldera_export_requires_executed_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    stack = Stack(tmp_path / "caldera-empty.db")
    operation = stack.operations.create(
        "RT empty", OperationMode.RED_TEAM, ScopeContract(includes=("example.com",))
    )
    stack.redteam.add_step(operation.id, "Planned only", "T1566")
    with pytest.raises(ReportError, match="no executed attack steps"):
        stack.exports.export_caldera(operation.id, tmp_path / "pack")


def test_vectr_export_structure_and_retired_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    stack = Stack(tmp_path / "vectr.db")
    operation = stack.operations.create(
        "Plan lab", OperationMode.OPERATION_PLAN,
        ScopeContract(includes=("example.com",)),
    )
    stack.planning.init_planning(operation.id, "Q1 campaign")
    stack.vault.add("plan-token", CANARY, operation.id)
    active = stack.planning.add_test_case(
        operation.id, "Recon sweep", "T1595", KillChainPhase.RECONNAISSANCE,
        tactic="reconnaissance", notes=f"token {CANARY}",
    )
    retired = stack.planning.add_test_case(
        operation.id, "Old test", "T1078", KillChainPhase.EXPLOITATION
    )
    stack.planning.update_test_case(retired.id, "retire")

    dest = tmp_path / "campaign.json"
    result = stack.exports.export_vectr(operation.id, dest)

    document = json.loads(dest.read_text(encoding="utf-8"))
    assert document["format"] == "vectr-campaign@v1"
    assert document["name"] == "Q1 campaign"
    ids = [case["id"] for case in document["test_cases"]]
    assert ids == [active.id]
    case = document["test_cases"][0]
    assert case["technique_id"] == "T1595"
    assert case["tactic"] == "reconnaissance"
    assert case["kill_chain_phase"] == "reconnaissance"
    assert result["files"][0]["sha256"]
    assert CANARY not in dest.read_text(encoding="utf-8")
    assert any(
        e.event_type == "export.generated" and e.payload["format"] == "vectr-campaign@v1"
        for e in stack.operations.events(operation.id)
    )


def test_dossier_html_escapes_and_redacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    stack = Stack(tmp_path / "dossier.db")
    operation = stack.operations.create(
        "Lab <script>alert(1)</script>",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("example.com",)),
    )
    stack.vault.add("dossier-token", CANARY, operation.id)
    stack.kernel.create_plan(operation.id, "Dossier plan", [], [])
    stack.kernel.create_finding(
        operation.id, f"Finding with {CANARY}", FindingSeverity.HIGH
    )

    dest = tmp_path / "dossier.html"
    result = stack.exports.export_dossier(operation.id, dest)

    text = dest.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text
    assert CANARY not in text
    assert "Event timeline" in text
    assert result["files"][0]["sha256"]
    assert any(
        e.event_type == "dossier.generated" for e in stack.operations.events(operation.id)
    )
    # dossier зарегистрирован как governed artifact
    artifacts = stack.kernel.kernel.list_artifacts(operation.id)
    assert any("dossier" in artifact.summary.lower() for artifact in artifacts)


def test_html_report_escapes_and_redacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    stack = Stack(tmp_path / "report.db")
    operation = stack.operations.create(
        "Bounty <b>lab</b>", OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )
    stack.playbooks.init_bug_bounty(operation.id)
    stack.vault.add("report-token", CANARY, operation.id)
    finding = stack.kernel.create_finding(
        operation.id, f"XSS via {CANARY}", FindingSeverity.HIGH,
        summary="<img onerror=x>",
    )
    artifact = stack.kernel.register_artifact(
        operation.id, ArtifactKind.NOTE, "poc", summary="poc"
    )
    stack.kernel.attach_evidence(finding.id, artifact.id)
    stack.kernel.validate_finding(finding.id)
    for status in (
        OperationStatus.PLANNING,
        OperationStatus.APPROVAL,
        OperationStatus.EXECUTION,
        OperationStatus.VERIFICATION,
    ):
        if status is OperationStatus.APPROVAL:
            stack.kernel.approve_plan(operation.id)
        stack.kernel.transition_operation(operation.id, status)

    report = stack.reports.generate(operation.id, format=ReportFormat.HTML)
    assert report.format is ReportFormat.HTML
    assert report.path.endswith(".html")
    content = (
        stack.reports.reports.report_root.parent / report.path
    ).read_text(encoding="utf-8")
    assert "<b>lab</b>" not in content
    assert "&lt;b&gt;" in content
    assert "<img onerror=x>" not in content
    assert CANARY not in content
    assert "Validated Findings" in content
