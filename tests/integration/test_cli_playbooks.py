import json
from pathlib import Path

from samurai_ai.cli import main


def _run_json(args: list[str], capsys) -> object:
    result = main(args)
    assert result == 0
    return json.loads(capsys.readouterr().out)


def _make_bounty_operation(database: str, capsys) -> str:
    operation = _run_json(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "Bounty CLI",
            "--mode",
            "bug-bounty",
            "--include",
            "*.example.com",
            "--exclude",
            "admin.example.com",
        ],
        capsys,
    )
    return operation["id"]


def _init_playbook(database: str, operation_id: str, capsys) -> None:
    assert (
        main(
            [
                "--db",
                database,
                "playbook",
                "init",
                operation_id,
                "--policy-url",
                "https://policy.example.com",
                "--safe-harbor",
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_cli_playbook_init_and_summary(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_bounty_operation(database, capsys)
    _init_playbook(database, operation_id, capsys)

    rules = _run_json(
        ["--db", database, "--json", "playbook", "show", operation_id], capsys
    )
    assert rules["policy_url"] == "https://policy.example.com"
    assert rules["safe_harbor"] is True

    summary = _run_json(
        ["--db", database, "--json", "playbook", "summary", operation_id], capsys
    )
    assert summary["findings_total"] == 0

    # one-shot guard
    assert main(["--db", database, "playbook", "init", operation_id]) == 2
    capsys.readouterr()


def test_cli_asset_ledger_flow(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_bounty_operation(database, capsys)
    _init_playbook(database, operation_id, capsys)

    assert (
        main(["--db", database, "asset", "add", operation_id, "domain", "API.example.COM."])
        == 0
    )
    capsys.readouterr()
    assets = _run_json(
        ["--db", database, "--json", "asset", "list", operation_id], capsys
    )
    assert [entry["value"] for entry in assets] == ["api.example.com"]

    # out-of-scope and excluded assets are refused with exit code 2
    assert (
        main(["--db", database, "asset", "add", operation_id, "domain", "other.org"]) == 2
    )
    assert (
        main(["--db", database, "asset", "add", operation_id, "domain", "admin.example.com"])
        == 2
    )
    capsys.readouterr()

    assert (
        main(["--db", database, "asset", "retire", operation_id, "domain", "api.example.com"])
        == 0
    )
    capsys.readouterr()
    diff = _run_json(
        ["--db", database, "--json", "asset", "diff", operation_id], capsys
    )
    assert [entry["value"] for entry in diff["removed"]] == ["api.example.com"]
    assert diff["added"] == []


def test_cli_finding_propose_dedup(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_bounty_operation(database, capsys)
    _init_playbook(database, operation_id, capsys)

    propose = [
        "--db",
        database,
        "--json",
        "finding",
        "propose",
        operation_id,
        "Reflected XSS",
        "--asset",
        "https://api.example.com/",
        "--category",
        "web",
    ]
    finding = _run_json(propose, capsys)
    # duplicate candidate is rejected
    assert main(propose) == 2
    capsys.readouterr()
    # forced duplicate is created and can be linked to the original
    duplicate = _run_json([*propose, "--force"], capsys)
    assert (
        main(["--db", database, "finding", "duplicate", duplicate["id"], finding["id"]]) == 0
    )
    capsys.readouterr()
