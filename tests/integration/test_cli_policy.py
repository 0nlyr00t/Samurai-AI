"""Operation role floors: the owner tightens, the global floor stays."""

from pathlib import Path

from samurai_ai.cli import main
from samurai_ai.storage.access_policy import OperationPolicyRepository
from samurai_ai.storage.database import Database


def make_operation(tmp_path: Path, capsys) -> tuple[str, str]:
    database = str(tmp_path / "policy.db")
    created = [
        "--db", database, "--json", "operation", "create", "Policy lab",
        "--mode", "bug-bounty", "--include", "example.com",
    ]
    assert main(created) == 0
    import json

    operation_id = json.loads(capsys.readouterr().out)["id"]
    return database, operation_id


def test_the_owner_sets_a_floor_and_it_is_enforced(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "policy", "set", operation_id, "report", "admin"]) == 0
    assert "set\treport\tadmin" in capsys.readouterr().out

    # admin is the local fallback principal, so it passes
    assert main(["--db", database, "policy", "list", operation_id]) == 0
    assert "report\tadmin" in capsys.readouterr().out

    rows = OperationPolicyRepository(Database(Path(database))).effective_paths(
        operation_id
    )
    assert rows["report"].value == "admin"


def test_removing_a_floor_reports_missing(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "policy", "rm", operation_id, "report"]) == 1
    assert "no floor" in capsys.readouterr().err
    assert main(["--db", database, "policy", "set", operation_id, "tool.run", "admin"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "policy", "rm", operation_id, "tool.run"]) == 0
    assert "removed" in capsys.readouterr().out


def test_a_floor_never_lowers_the_global_requirement() -> None:
    from samurai_ai.domain.access import Role, combined_minimum

    # report needs operator globally; a viewer floor must not weaken it
    assert combined_minimum("report", Role.VIEWER) is Role.OPERATOR
    assert combined_minimum("report", Role.ADMIN) is Role.ADMIN
    assert combined_minimum("report", None) is Role.OPERATOR
