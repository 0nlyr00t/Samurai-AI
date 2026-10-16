import json
from pathlib import Path

from samurai_ai.cli import main


def test_cli_operation_lifecycle_and_scope(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    create_result = main(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "CLI lab",
            "--mode",
            "bug-bounty",
            "--objective",
            "Validate a report",
            "--include",
            "*.example.com",
            "--exclude",
            "api.example.com",
        ]
    )
    assert create_result == 0
    operation = json.loads(capsys.readouterr().out)
    operation_id = operation["id"]
    assert operation["status"] == "draft"

    assert main(["--db", database, "operation", "transition", operation_id, "planning"]) == 0
    assert "planning" in capsys.readouterr().out

    assert main(["--db", database, "--json", "operation", "show", operation_id, "--events"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert len(shown["events"]) == 2

    assert main(["--db", database, "scope", "check", operation_id, "www.example.com"]) == 0
    assert "ALLOW" in capsys.readouterr().out

    assert main(["--db", database, "scope", "check", operation_id, "api.example.com"]) == 1
    assert "DENY" in capsys.readouterr().out

    assert main(["--db", database, "--json", "operation", "list"]) == 0
    operations = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in operations] == [operation_id]


def test_cli_returns_an_error_for_invalid_transition(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli-errors.db")
    assert main(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "CLI errors",
            "--mode",
            "ctf-htb",
            "--include",
            "192.0.2.10",
        ]
    ) == 0
    operation_id = json.loads(capsys.readouterr().out)["id"]

    assert main(["--db", database, "operation", "transition", operation_id, "execution"]) == 2
    captured = capsys.readouterr()
    assert "transition from draft to execution is not allowed" in captured.err
