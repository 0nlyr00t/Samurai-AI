import json
from pathlib import Path

from samurai_ai.cli import main


def _run_json(args: list[str], capsys) -> object:
    result = main(args)
    assert result == 0
    return json.loads(capsys.readouterr().out)


def _make_plan_operation(database: str, capsys) -> str:
    operation = _run_json(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "Plan CLI",
            "--mode",
            "operation-plan",
            "--include",
            "lab.local",
        ],
        capsys,
    )
    return operation["id"]


def _init_scenario(database: str, operation_id: str, capsys) -> None:
    assert (
        main(
            [
                "--db",
                database,
                "scenario",
                "init",
                operation_id,
                "--name",
                "Q3 emulation",
                "--schedule",
                "weekdays 09:00-12:00",
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_cli_scenario_init_show_summary(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_plan_operation(database, capsys)
    _init_scenario(database, operation_id, capsys)

    scenario = _run_json(
        ["--db", database, "--json", "scenario", "show", operation_id], capsys
    )
    assert scenario["name"] == "Q3 emulation"
    assert scenario["schedule"] == "weekdays 09:00-12:00"

    summary = _run_json(
        ["--db", database, "--json", "scenario", "summary", operation_id], capsys
    )
    assert summary["test_cases_draft"] == 0
    assert summary["runbooks"] == 0

    # one-shot guard
    assert (
        main(
            ["--db", database, "scenario", "init", operation_id, "--name", "again"]
        )
        == 2
    )
    capsys.readouterr()


def test_cli_test_case_flow(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_plan_operation(database, capsys)
    _init_scenario(database, operation_id, capsys)

    first = _run_json(
        [
            "--db",
            database,
            "--json",
            "test-case",
            "add",
            operation_id,
            "--name",
            "passive recon",
            "--technique",
            "T1595.002",
            "--phase",
            "reconnaissance",
        ],
        capsys,
    )
    second = _run_json(
        [
            "--db",
            database,
            "--json",
            "test-case",
            "add",
            operation_id,
            "--name",
            "spearphish",
            "--technique",
            "T1566.001",
            "--phase",
            "delivery",
            "--depends-on",
            first["id"],
            "--stop-before",
        ],
        capsys,
    )
    assert second["depends_on"] == [first["id"]]
    assert second["stop_before"] is True

    # unknown dependency rejected
    assert (
        main(
            [
                "--db",
                database,
                "test-case",
                "add",
                operation_id,
                "--name",
                "bad",
                "--technique",
                "T1003",
                "--phase",
                "installation",
                "--depends-on",
                "tc_missing",
            ]
        )
        == 2
    )
    capsys.readouterr()

    ready = _run_json(
        [
            "--db",
            database,
            "--json",
            "test-case",
            "update",
            first["id"],
            "--status",
            "ready",
        ],
        capsys,
    )
    assert ready["status"] == "ready"

    listed = _run_json(
        ["--db", database, "--json", "test-case", "list", operation_id], capsys
    )
    assert len(listed) == 2

    assert main(["--db", database, "scenario", "map", operation_id]) == 0
    map_text = capsys.readouterr().out
    assert map_text.index("passive recon") < map_text.index("spearphish")
    assert "[STOP POINT]" in map_text


def test_cli_runbook_in_planning_status(tmp_path: Path, capsys) -> None:
    """Planning mode: runbook is produced without entering execution."""
    database = str(tmp_path / "cli.db")
    operation_id = _make_plan_operation(database, capsys)
    _init_scenario(database, operation_id, capsys)
    case = _run_json(
        [
            "--db",
            database,
            "--json",
            "test-case",
            "add",
            operation_id,
            "--name",
            "passive recon",
            "--technique",
            "T1595.002",
            "--phase",
            "reconnaissance",
        ],
        capsys,
    )
    assert (
        main(
            [
                "--db",
                database,
                "test-case",
                "update",
                case["id"],
                "--status",
                "ready",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(["--db", database, "operation", "transition", operation_id, "planning"])
        == 0
    )
    capsys.readouterr()

    runbook = _run_json(
        ["--db", database, "--json", "runbook", "generate", operation_id], capsys
    )
    text = (tmp_path / runbook["path"]).read_text(encoding="utf-8")
    assert "Operation Runbook: Q3 emulation" in text
    assert "T1595.002" in text

    runbooks = _run_json(
        ["--db", database, "--json", "runbook", "list", operation_id], capsys
    )
    assert [item["id"] for item in runbooks] == [runbook["id"]]

    assert (
        main(["--db", database, "runbook", "show", operation_id, runbook["id"]]) == 0
    )
    assert "passive recon" in capsys.readouterr().out

    dest = tmp_path / "export"
    manifest = _run_json(
        [
            "--db",
            database,
            "--json",
            "runbook",
            "export",
            operation_id,
            runbook["id"],
            str(dest),
        ],
        capsys,
    )
    assert manifest["report_id"] == runbook["id"]
    assert (dest / "runbook.md").exists()


def test_cli_runbook_guards(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_plan_operation(database, capsys)
    _init_scenario(database, operation_id, capsys)

    # DRAFT status is not allowed
    assert main(["--db", database, "runbook", "generate", operation_id]) == 2
    capsys.readouterr()
