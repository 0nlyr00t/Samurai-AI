"""`samurai finding impact`: the ladder rules hold at the CLI boundary too."""

import json
from pathlib import Path

import pytest

from samurai_ai.cli import main


def make_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> tuple[str, str]:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    database = str(tmp_path / "impact.db")
    created = [
        "--db",
        database,
        "--json",
        "operation",
        "create",
        "Lab",
        "--mode",
        "bug-bounty",
        "--include",
        "example.com",
    ]
    assert main(created) == 0
    operation_id = json.loads(capsys.readouterr().out)["id"]
    added = [
        "--db",
        database,
        "--json",
        "artifact",
        "add",
        operation_id,
        "--kind",
        "command-output",
        "--text",
        "proof",
    ]
    assert main(added) == 0
    artifact_id = json.loads(capsys.readouterr().out)["id"]
    assert main(["--db", database, "--json", "finding", "create", operation_id, "IDOR"]) == 0
    finding_id = json.loads(capsys.readouterr().out)["id"]
    assert main(["--db", database, "finding", "attach", finding_id, artifact_id]) == 0
    return finding_id, artifact_id


def test_an_attempt_needs_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    finding_id, _ = make_finding(tmp_path, monkeypatch, capsys)
    database = str(tmp_path / "impact.db")
    with pytest.raises(SystemExit):
        main(["--db", database, "finding", "impact", "attempt", finding_id, "read"])
    assert "required" in capsys.readouterr().err


def test_a_state_change_needs_a_rollback_note(tmp_path: Path, monkeypatch, capsys) -> None:
    finding_id, artifact_id = make_finding(tmp_path, monkeypatch, capsys)
    database = str(tmp_path / "impact.db")
    attempt = [
        "--db",
        database,
        "finding",
        "impact",
        "attempt",
        finding_id,
        "write",
        "--evidence",
        artifact_id,
    ]
    assert main(attempt) == 2
    assert "rollback" in capsys.readouterr().err


def test_grade_reports_the_level_and_the_next_step(tmp_path: Path, monkeypatch, capsys) -> None:
    finding_id, artifact_id = make_finding(tmp_path, monkeypatch, capsys)
    database = str(tmp_path / "impact.db")
    attempt = [
        "--db",
        database,
        "finding",
        "impact",
        "attempt",
        finding_id,
        "read",
        "--evidence",
        artifact_id,
    ]
    assert main(attempt) == 0
    assert main(["--db", database, "finding", "impact", "grade", finding_id]) == 0
    out = capsys.readouterr().out
    assert "level=read" in out and "next=write" in out and "reportable=no" in out
