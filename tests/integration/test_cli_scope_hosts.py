"""Host mappings: a vhost is only useful if the mapper keeps it honest."""

import json
from pathlib import Path

from samurai_ai.cli import main


def make_operation(tmp_path: Path, capsys) -> tuple[str, str]:
    database = str(tmp_path / "hosts.db")
    assert (
        main(
            ["--db", database, "--json", "operation", "create", "Hosts",
             "--mode", "red-team", "--include", "10.129.98.19"]
        )
        == 0
    )
    return database, json.loads(capsys.readouterr().out)["id"]


def test_a_mapping_is_listed_and_checks_as_its_address(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "scope", "host", "add", operation_id,
                 "flight.htb", "10.129.98.19"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "scope", "host", "list", operation_id]) == 0
    assert "flight.htb\t10.129.98.19" in capsys.readouterr().out
    assert main(["--db", database, "scope", "check", operation_id,
                 "http://flight.htb/"]) == 0
    assert "ALLOW" in capsys.readouterr().out


def test_an_out_of_scope_target_warns_but_stays(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "scope", "host", "add", operation_id,
                 "elsewhere.htb", "10.10.10.10"]) == 0
    assert "out of scope" in capsys.readouterr().err
    assert main(["--db", database, "--json", "operation", "show", operation_id]) == 0
    assert "elsewhere.htb" in json.loads(capsys.readouterr().out)["scope"]["hosts"]


def test_removing_an_absent_mapping_is_an_error(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "scope", "host", "rm", operation_id, "nope"]) == 1
    assert "no mapping" in capsys.readouterr().err


def test_a_bad_mapping_is_refused(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "scope", "host", "add", operation_id,
                 "10.0.0.2", "10.129.98.19"]) == 2
    assert "must be a name" in capsys.readouterr().err
