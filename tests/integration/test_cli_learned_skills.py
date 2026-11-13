"""`samurai learned-skill`: inspect and gate agent-distilled skills from the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.cli import _make_services, main
from samurai_ai.domain.execution import ToolRun, ToolRunStatus
from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity, PlanTask
from samurai_ai.domain.learned_skills import LearnedSkillStatus
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.storage.execution import ToolRunRepository

WORKSPACE = "lab"


def use_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))


def _seed_confirmed_skill() -> str:
    """Create an operation with a confirmed finding + a command, then extract."""
    services = _make_services(None)
    operations, kernel, learned = services[0], services[1], services[-1]
    operation = operations.create("lab", OperationMode.CTF_HTB, ScopeContract(includes=("x",)))
    kernel.create_plan(operation.id, "plan", [PlanTask(id="probe", title="Probe")], [])
    artifact = kernel.register_artifact(operation.id, ArtifactKind.COMMAND_OUTPUT, "body")
    finding = kernel.create_finding(operation.id, "SMB null session", FindingSeverity.MEDIUM)
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.validate_finding(finding.id)
    ToolRunRepository(learned.repo.database).add_run(
        ToolRun(
            operation_id=operation.id,
            task_id="probe",
            tool_id="shell.exec",
            tool_version=1,
            parameters={"command": "enum4linux -a host"},
            status=ToolRunStatus.COMPLETED,
        ),
        OperationEvent(operation_id=operation.id, event_type="tool.run.completed"),
    )
    skill = learned.extract(operation.id, WORKSPACE)[0]
    return skill.id


def test_mode_defaults_to_shadow_and_can_be_set(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_home(monkeypatch, tmp_path)
    assert main(["learned-skill", "mode"]) == 0
    assert capsys.readouterr().out.strip() == "shadow"
    assert main(["learned-skill", "mode", "inject"]) == 0
    assert capsys.readouterr().out.strip() == "inject"


def test_list_is_empty_before_anything_is_learned(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_home(monkeypatch, tmp_path)
    assert main(["learned-skill", "list"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_extracted_skill_lists_then_promotes(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_home(monkeypatch, tmp_path)
    skill_id = _seed_confirmed_skill()

    assert main(["learned-skill", "list"]) == 0
    listed = capsys.readouterr().out
    assert skill_id in listed
    assert "shadow" in listed

    assert main(["learned-skill", "promote", skill_id]) == 0
    assert "active" in capsys.readouterr().out

    services = _make_services(None)
    assert services[-1].get(skill_id).status is LearnedSkillStatus.ACTIVE
