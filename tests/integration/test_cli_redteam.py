import json
from pathlib import Path

from samurai_ai.cli import main


def _run_json(args: list[str], capsys) -> object:
    result = main(args)
    assert result == 0
    return json.loads(capsys.readouterr().out)


def _make_rt_operation(database: str, capsys) -> str:
    operation = _run_json(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "RT CLI",
            "--mode",
            "red-team",
            "--include",
            "corp.local",
        ],
        capsys,
    )
    return operation["id"]


def _init_roe(database: str, operation_id: str, capsys) -> None:
    assert (
        main(
            [
                "--db",
                database,
                "roe",
                "init",
                operation_id,
                "--threat-profile",
                "APT29-style",
                "--objective",
                "reach DA",
                "--success-criteria",
                "DA with detection notes",
            ]
        )
        == 0
    )
    capsys.readouterr()


def _approve_pending(database: str, operation_id: str, capsys) -> None:
    approvals = _run_json(
        ["--db", database, "--json", "approval", "list", operation_id, "--pending"],
        capsys,
    )
    for approval in approvals:
        assert main(["--db", database, "approval", "approve", approval["id"]]) == 0
        capsys.readouterr()


def _drive_to_verification(database: str, operation_id: str, capsys) -> None:
    for status in ("planning", "approval"):
        assert (
            main(["--db", database, "operation", "transition", operation_id, status])
            == 0
        )
        capsys.readouterr()
    assert main(["--db", database, "plan", "approve", operation_id]) == 0
    capsys.readouterr()
    _approve_pending(database, operation_id, capsys)
    for status in ("execution", "verification"):
        assert (
            main(["--db", database, "operation", "transition", operation_id, status])
            == 0
        )
        capsys.readouterr()


def test_cli_roe_init_show_and_one_shot(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_rt_operation(database, capsys)
    _init_roe(database, operation_id, capsys)

    roe = _run_json(["--db", database, "--json", "roe", "show", operation_id], capsys)
    assert roe["threat_profile"] == "APT29-style"
    assert roe["objectives"] == ["reach DA"]

    # one-shot guard
    assert (
        main(
            [
                "--db",
                database,
                "roe",
                "init",
                operation_id,
                "--threat-profile",
                "again",
                "--objective",
                "x",
            ]
        )
        == 2
    )
    capsys.readouterr()


def test_cli_execution_blocked_until_roe_approval(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_rt_operation(database, capsys)
    _init_roe(database, operation_id, capsys)
    for status in ("planning", "approval"):
        assert (
            main(["--db", database, "operation", "transition", operation_id, status])
            == 0
        )
        capsys.readouterr()
    assert main(["--db", database, "plan", "approve", operation_id]) == 0
    capsys.readouterr()

    # pending ROE approval blocks execution (BRD scenario 4)
    assert (
        main(["--db", database, "operation", "transition", operation_id, "execution"])
        == 2
    )
    capsys.readouterr()
    _approve_pending(database, operation_id, capsys)
    assert (
        main(["--db", database, "operation", "transition", operation_id, "execution"])
        == 0
    )
    capsys.readouterr()


def test_cli_attack_path_flow(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_rt_operation(database, capsys)
    _init_roe(database, operation_id, capsys)

    first = _run_json(
        [
            "--db",
            database,
            "--json",
            "attack-path",
            "add",
            operation_id,
            "--name",
            "phish",
            "--technique",
            "T1566",
            "--expected-telemetry",
            "process creation",
        ],
        capsys,
    )
    second = _run_json(
        [
            "--db",
            database,
            "--json",
            "attack-path",
            "add",
            operation_id,
            "--name",
            "dump creds",
            "--technique",
            "T1003",
            "--depends-on",
            first["id"],
        ],
        capsys,
    )
    assert second["depends_on"] == [first["id"]]

    # unknown dependency rejected
    assert (
        main(
            [
                "--db",
                database,
                "attack-path",
                "add",
                operation_id,
                "--name",
                "lateral",
                "--technique",
                "T1021",
                "--depends-on",
                "step_missing",
            ]
        )
        == 2
    )
    capsys.readouterr()

    running = _run_json(
        [
            "--db",
            database,
            "--json",
            "attack-path",
            "update",
            first["id"],
            "--status",
            "executing",
        ],
        capsys,
    )
    assert running["status"] == "executing"
    done = _run_json(
        [
            "--db",
            database,
            "--json",
            "attack-path",
            "update",
            first["id"],
            "--status",
            "executed",
        ],
        capsys,
    )
    assert done["status"] == "executed"
    detected = _run_json(
        [
            "--db",
            database,
            "--json",
            "attack-path",
            "detection",
            first["id"],
            "--result",
            "detected",
            "--notes",
            "EDR fired",
        ],
        capsys,
    )
    assert detected["detection"] == "detected"

    listed = _run_json(
        ["--db", database, "--json", "attack-path", "list", operation_id], capsys
    )
    assert len(listed) == 2

    assert main(["--db", database, "attack-path", "show", operation_id]) == 0
    path_text = capsys.readouterr().out
    assert path_text.index("phish") < path_text.index("dump creds")

    summary = _run_json(
        ["--db", database, "--json", "attack-path", "summary", operation_id], capsys
    )
    assert summary["steps_executed"] == 1
    assert summary["steps_planned"] == 1


def test_cli_checkpoint_flow(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_rt_operation(database, capsys)
    _init_roe(database, operation_id, capsys)

    checkpoint = _run_json(
        [
            "--db",
            database,
            "--json",
            "checkpoint",
            "add",
            operation_id,
            "--name",
            "Before DA",
            "--criterion",
            "scope window confirmed",
        ],
        capsys,
    )
    reached = _run_json(
        ["--db", database, "--json", "checkpoint", "reach", checkpoint["id"]], capsys
    )
    assert reached["status"] == "reached"

    # second reach fails
    assert main(["--db", database, "checkpoint", "reach", checkpoint["id"]]) == 2
    capsys.readouterr()

    listed = _run_json(
        ["--db", database, "--json", "checkpoint", "list", operation_id], capsys
    )
    assert [item["id"] for item in listed] == [checkpoint["id"]]


def test_cli_debrief_flow(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_rt_operation(database, capsys)
    _init_roe(database, operation_id, capsys)
    step = _run_json(
        [
            "--db",
            database,
            "--json",
            "attack-path",
            "add",
            operation_id,
            "--name",
            "phish",
            "--technique",
            "T1566",
        ],
        capsys,
    )
    for status in ("executing", "executed"):
        assert (
            main(
                [
                    "--db",
                    database,
                    "attack-path",
                    "update",
                    step["id"],
                    "--status",
                    status,
                ]
            )
            == 0
        )
        capsys.readouterr()
    _drive_to_verification(database, operation_id, capsys)

    debrief = _run_json(
        ["--db", database, "--json", "debrief", "generate", operation_id], capsys
    )
    text = (tmp_path / debrief["path"]).read_text(encoding="utf-8")
    assert "Red Team Debrief" in text
    assert "T1566" in text

    debriefs = _run_json(
        ["--db", database, "--json", "debrief", "list", operation_id], capsys
    )
    assert [item["id"] for item in debriefs] == [debrief["id"]]

    assert (
        main(["--db", database, "debrief", "show", operation_id, debrief["id"]]) == 0
    )
    assert "phish" in capsys.readouterr().out

    dest = tmp_path / "export"
    manifest = _run_json(
        [
            "--db",
            database,
            "--json",
            "debrief",
            "export",
            operation_id,
            debrief["id"],
            str(dest),
        ],
        capsys,
    )
    assert manifest["report_id"] == debrief["id"]
    assert (dest / "debrief.md").exists()


def test_cli_debrief_requires_steps_and_status(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_rt_operation(database, capsys)
    _init_roe(database, operation_id, capsys)
    assert main(["--db", database, "debrief", "generate", operation_id]) == 2
    capsys.readouterr()
