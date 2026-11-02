"""Membership over the CLI: the creator owns it and strangers stay out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from samurai_ai.cli import main

TEST_KEY = "ef" * 32


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    return tmp_path


def test_the_creator_owns_the_operation(home: Path, capsys) -> None:
    database = str(home / "a.db")
    assert main(["--db", database, "--json", "operation", "create", "Lab",
                 "--mode", "bug-bounty", "--include", "a.test"]) == 0
    operation_id = json.loads(capsys.readouterr().out)["id"]

    assert main(["--db", database, "team", "members", operation_id]) == 0
    out = capsys.readouterr().out
    assert "owner: local" in out
    assert "owner" in out


def test_a_note_is_recorded(home: Path, capsys) -> None:
    database = str(home / "b.db")
    assert main(["--db", database, "--json", "operation", "create", "Lab",
                 "--mode", "bug-bounty", "--include", "a.test"]) == 0
    operation_id = json.loads(capsys.readouterr().out)["id"]

    assert main(["--db", database, "team", "note", operation_id, "finding", "fnd_1",
                 "try id 2"]) == 0
    assert "note note_" in capsys.readouterr().out


def test_an_unknown_subject_is_refused(home: Path, capsys) -> None:
    database = str(home / "c.db")
    assert main(["--db", database, "--json", "operation", "create", "Lab",
                 "--mode", "bug-bounty", "--include", "a.test"]) == 0
    operation_id = json.loads(capsys.readouterr().out)["id"]

    assert main(["--db", database, "team", "add", operation_id, "s-ghost"]) == 2
    assert "has signed in" in capsys.readouterr().err
