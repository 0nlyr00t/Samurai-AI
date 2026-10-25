import json
from pathlib import Path

from samurai_ai.cli import main


def _run_json(args: list[str], capsys) -> object:
    result = main(args)
    assert result == 0
    return json.loads(capsys.readouterr().out)


def _make_ctf_operation(database: str, capsys) -> str:
    operation = _run_json(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "HTB CLI",
            "--mode",
            "ctf-htb",
            "--include",
            "10.10.10.0/24",
        ],
        capsys,
    )
    return operation["id"]


def _init_challenge(database: str, operation_id: str, capsys) -> None:
    assert (
        main(
            [
                "--db",
                database,
                "challenge",
                "init",
                operation_id,
                "--name",
                "BabyRE",
                "--category",
                "reverse",
                "--flag-format",
                r"HTB\{[a-z0-9_]+\}",
                "--win-condition",
                "user and root flags",
            ]
        )
        == 0
    )
    capsys.readouterr()


def _drive_to_verification(database: str, operation_id: str, capsys) -> None:
    for status in ("planning", "approval"):
        assert (
            main(["--db", database, "operation", "transition", operation_id, status]) == 0
        )
        capsys.readouterr()
    assert main(["--db", database, "plan", "approve", operation_id]) == 0
    capsys.readouterr()
    for status in ("execution", "verification"):
        assert (
            main(["--db", database, "operation", "transition", operation_id, status]) == 0
        )
        capsys.readouterr()


def test_cli_challenge_init_show_summary(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_ctf_operation(database, capsys)
    _init_challenge(database, operation_id, capsys)

    challenge = _run_json(
        ["--db", database, "--json", "challenge", "show", operation_id], capsys
    )
    assert challenge["name"] == "BabyRE"
    assert challenge["category"] == "reverse"

    summary = _run_json(
        ["--db", database, "--json", "challenge", "summary", operation_id], capsys
    )
    assert summary["flags_captured"] == 0
    assert summary["hypotheses_open"] == 0

    # one-shot guard
    assert (
        main(
            [
                "--db",
                database,
                "challenge",
                "init",
                operation_id,
                "--name",
                "Again",
                "--category",
                "web",
            ]
        )
        == 2
    )
    capsys.readouterr()


def test_cli_hypothesis_flow(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_ctf_operation(database, capsys)
    _init_challenge(database, operation_id, capsys)

    hypothesis = _run_json(
        [
            "--db",
            database,
            "--json",
            "hypothesis",
            "add",
            operation_id,
            "BOF in name field",
            "--category",
            "pwn",
        ],
        capsys,
    )
    assert hypothesis["status"] == "open"

    updated = _run_json(
        [
            "--db",
            database,
            "--json",
            "hypothesis",
            "update",
            hypothesis["id"],
            "--status",
            "testing",
        ],
        capsys,
    )
    assert updated["status"] == "testing"

    confirmed = _run_json(
        [
            "--db",
            database,
            "--json",
            "hypothesis",
            "update",
            hypothesis["id"],
            "--status",
            "confirmed",
            "--notes",
            "ROP chain works",
        ],
        capsys,
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["notes"] == "ROP chain works"

    listed = _run_json(
        ["--db", database, "--json", "hypothesis", "list", operation_id], capsys
    )
    assert [item["id"] for item in listed] == [hypothesis["id"]]

    # invalid transition from confirmed
    assert (
        main(
            [
                "--db",
                database,
                "hypothesis",
                "update",
                hypothesis["id"],
                "--status",
                "abandoned",
            ]
        )
        == 2
    )
    capsys.readouterr()


def test_cli_flag_ledger(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_ctf_operation(database, capsys)
    _init_challenge(database, operation_id, capsys)

    record = _run_json(
        [
            "--db",
            database,
            "--json",
            "flag",
            "add",
            operation_id,
            "--name",
            "user",
            "--value",
            "HTB{u53r_fl4g}",
        ],
        capsys,
    )
    assert record["name"] == "user"

    flags = _run_json(
        ["--db", database, "--json", "flag", "list", operation_id], capsys
    )
    assert [item["value"] for item in flags] == ["HTB{u53r_fl4g}"]

    # format mismatch rejected
    assert (
        main(
            [
                "--db",
                database,
                "flag",
                "add",
                operation_id,
                "--name",
                "root",
                "--value",
                "flag{nope}",
            ]
        )
        == 2
    )
    capsys.readouterr()

    # duplicate name rejected
    assert (
        main(
            [
                "--db",
                database,
                "flag",
                "add",
                operation_id,
                "--name",
                "user",
                "--value",
                "HTB{an0ther}",
            ]
        )
        == 2
    )
    capsys.readouterr()


def test_cli_writeup_and_replay(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_ctf_operation(database, capsys)
    _init_challenge(database, operation_id, capsys)
    assert (
        main(
            [
                "--db",
                database,
                "flag",
                "add",
                operation_id,
                "--name",
                "user",
                "--value",
                "HTB{u53r_fl4g}",
            ]
        )
        == 0
    )
    capsys.readouterr()
    _drive_to_verification(database, operation_id, capsys)

    writeup = _run_json(
        ["--db", database, "--json", "writeup", "generate", operation_id], capsys
    )
    text = (tmp_path / writeup["path"]).read_text(encoding="utf-8")
    assert "HTB{u53r_fl4g}" in text

    writeups = _run_json(
        ["--db", database, "--json", "writeup", "list", operation_id], capsys
    )
    assert [item["id"] for item in writeups] == [writeup["id"]]

    assert (
        main(["--db", database, "writeup", "show", operation_id, writeup["id"]]) == 0
    )
    shown = capsys.readouterr().out
    assert "CTF/HTB Writeup: BabyRE" in shown

    dest = tmp_path / "export"
    manifest = _run_json(
        [
            "--db",
            database,
            "--json",
            "writeup",
            "export",
            operation_id,
            writeup["id"],
            str(dest),
        ],
        capsys,
    )
    assert manifest["report_id"] == writeup["id"]
    assert (dest / "writeup.md").exists()

    assert main(["--db", database, "replay", "show", operation_id]) == 0
    replay = capsys.readouterr().out
    assert "Solution Replay" in replay
    assert "flag.captured" in replay


def test_cli_writeup_requires_flag_and_status(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id = _make_ctf_operation(database, capsys)
    _init_challenge(database, operation_id, capsys)

    # no flags and wrong status
    assert main(["--db", database, "writeup", "generate", operation_id]) == 2
    capsys.readouterr()
