"""Widening the scope is a decision; narrowing and repeating are not."""

import json
from pathlib import Path

import pytest

from samurai_ai.cli import main


def make_operation(tmp_path: Path, capsys) -> tuple[str, str]:
    database = str(tmp_path / "gate.db")
    assert main(["--db", database, "--json", "operation", "create", "Gate",
                 "--mode", "ctf-htb", "--include", "10.10.10.0/24"]) == 0
    return database, json.loads(capsys.readouterr().out)["id"]


def includes(database: str, operation_id: str, capsys) -> list[str]:
    capsys.readouterr()
    assert main(["--db", database, "--json", "operation", "show", operation_id]) == 0
    return json.loads(capsys.readouterr().out)["scope"]["includes"]


def test_a_covered_target_is_added_without_a_question(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def refuse(_: str) -> str:
        raise AssertionError("a covered target must not prompt")

    monkeypatch.setattr("builtins.input", refuse)
    assert main(["--db", database, "scope", "add", operation_id, "10.10.10.7"]) == 0
    assert "10.10.10.7" in includes(database, operation_id, capsys)


def test_without_a_terminal_a_widening_is_reported_not_asked(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline must not hang on a prompt nobody can answer."""
    database, operation_id = make_operation(tmp_path, capsys)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("no prompt without a tty"))
    assert main(["--db", database, "scope", "add", operation_id, "10.10.20.0/24"]) == 0
    assert "widened: 10.10.20.0/24" in capsys.readouterr().err
    assert "10.10.20.0/24" in includes(database, operation_id, capsys)


def test_a_declined_widening_leaves_the_scope_alone(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["--db", database, "scope", "add", operation_id, "example.com"]) == 0
    capt = capsys.readouterr()
    assert "not added: example.com" in capt.err
    assert "example.com" not in capt.out
    assert "example.com" not in includes(database, operation_id, capsys)


def test_yes_and_the_russian_yes_both_widen(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert main(["--db", database, "scope", "add", "--yes", operation_id, "a.test"]) == 0
    monkeypatch.setattr("builtins.input", lambda _: "да")
    assert main(["--db", database, "scope", "add", operation_id, "b.test"]) == 0
    assert includes(database, operation_id, capsys)[-2:] == ["a.test", "b.test"]
