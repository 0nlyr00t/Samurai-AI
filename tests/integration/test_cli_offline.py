"""Air-gapped mode through the CLI: the flag, the refusals, the report."""

import json
from pathlib import Path

from samurai_ai.cli import main


def offset(database: str, capsys, offline: bool = True) -> str:
    args = ["--db", database, "--json", "operation", "create", "Airgap",
            "--mode", "red-team", "--include", "10.10.10.5"]
    if offline:
        args.append("--offline")
    assert main(args) == 0
    return json.loads(capsys.readouterr().out)["id"]


def test_the_flag_is_recorded_and_shown(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "offline.db")
    operation_id = offset(database, capsys)
    assert main(["--db", database, "operation", "show", operation_id]) == 0
    assert "offline" in capsys.readouterr().out


def test_an_outside_tool_is_refused(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "offline.db")
    operation_id = offset(database, capsys)
    try:
        main(["--db", database, "tool", "run", operation_id,
              "recon.subfinder-passive", "--param", "domain=example.com"])
    except Exception as exc:  # noqa: BLE001 - the CLI surfaces the service error
        assert "needs the internet" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an internet tool ran offline")


def test_the_same_tool_runs_when_the_operation_is_online(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "online.db")
    operation_id = offset(database, capsys, offline=False)
    # the refusal never mentions the internet for an online operation
    try:
        main(["--db", database, "tool", "run", operation_id,
              "recon.subfinder-passive", "--param", "domain=example.com"])
    except Exception as exc:  # noqa: BLE001 - Docker may be absent
        assert "needs the internet" not in str(exc)


def test_the_check_command_reports_what_is_blocked(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "offline.db")
    operation_id = offset(database, capsys)
    capsys.readouterr()
    assert main(["--db", database, "offline", "check", operation_id]) in (0, 1)
    out = capsys.readouterr().out
    assert "refused tools" in out and "research.read" in out
    assert "local models" in out


def test_the_check_command_says_an_online_operation_is_not_offline(
    tmp_path: Path, capsys
) -> None:
    database = str(tmp_path / "online.db")
    operation_id = offset(database, capsys, offline=False)
    capsys.readouterr()
    assert main(["--db", database, "offline", "check", operation_id]) == 0
    assert "not offline" in capsys.readouterr().out
