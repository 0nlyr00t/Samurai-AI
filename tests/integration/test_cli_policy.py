"""Operation role floors: the owner tightens, the global floor stays."""

from pathlib import Path

import pytest

from samurai_ai.cli import main
from samurai_ai.domain.kernel import ArtifactKind
from samurai_ai.services.kernel import FindingNotFoundError
from samurai_ai.storage.access_policy import OperationPolicyRepository
from samurai_ai.storage.database import Database


def _kernel(database: str):
    from samurai_ai.services.kernel import KernelService
    from samurai_ai.services.operations import OperationService
    from samurai_ai.storage.database import Database as Db
    from samurai_ai.storage.kernel import KernelRepository
    from samurai_ai.storage.operations import OperationRepository

    store = Db(Path(database))
    return KernelService(
        OperationService(OperationRepository(store)), KernelRepository(store)
    )


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


def test_a_floor_follows_the_finding_id(tmp_path: Path, capsys) -> None:
    """`finding show <id>` carries no operation, so the id must supply it."""
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "policy", "set", operation_id, "finding.validate", "admin"]) == 0
    capsys.readouterr()

    kernel = _kernel(database)
    finding = kernel.create_finding(operation_id, "TLS on the edge")
    artifact = kernel.register_artifact(operation_id, ArtifactKind.LOG, "handshake ok")
    kernel.attach_evidence(finding.id, artifact.id)
    assert main(["--db", database, "finding", "validate", finding.id]) == 0
    assert "validated" in capsys.readouterr().out.lower()


def test_an_unknown_finding_id_falls_back_to_the_global_floor(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "policy", "set", operation_id, "finding.validate", "admin"]) == 0
    capsys.readouterr()
    # the id resolves to nothing, so the command reports the id rather than access
    with pytest.raises(FindingNotFoundError, match="nope"):
        main(["--db", database, "finding", "validate", "nope"])
