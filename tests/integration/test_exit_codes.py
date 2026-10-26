"""Stable CLI exit code contract (pinned by tests, documented in README).

0 — success;
1 — negative domain outcome (scope denied, agent run not completed,
    swarm partial/failed, scope check DENY);
2 — usage/domain/approval error (unknown ids, state errors, approval
    required, validation errors).
"""

import json
from pathlib import Path

import pytest

from samurai_ai.cli import main


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    return tmp_path


def make_operation(database: str, capsys) -> str:
    assert (
        main(
            [
                "--db", database, "--json", "operation", "create", "Codes",
                "--mode", "bug-bounty", "--include", "example.com",
            ]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)["id"]


def test_exit_code_contract(home: Path, capsys) -> None:
    database = str(home / "codes.db")
    operation_id = make_operation(database, capsys)

    # 0 — success
    assert main(["--db", database, "operation", "list"]) == 0
    capsys.readouterr()

    # 1 — negative domain outcome: scope check denies an out-of-scope target
    assert main(["--db", database, "scope", "check", operation_id, "evil.com"]) == 1
    assert "DENY" in capsys.readouterr().out

    # 2 — usage/domain errors
    assert main(["--db", database, "operation", "show", "op_missing"]) == 2
    assert "not found" in capsys.readouterr().err
    assert main(["--db", database, "swarm", "show", "swarm_missing"]) == 2
    capsys.readouterr()
    assert main(["--db", database, "memory", "show", "mem_missing"]) == 2
    capsys.readouterr()

    # 2 — approval required is a precondition error with operator guidance
    plan = {
        "title": "p",
        "tasks": [
            {"id": "recon", "title": "R", "risk_class": "r1",
             "depends_on": [], "required_artifacts": []}
        ],
        "gates": [],
    }
    plan_path = home / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["--db", database, "plan", "create", operation_id, str(plan_path)]) == 0
    capsys.readouterr()
    assert (
        main(["--db", database, "swarm", "run", operation_id, "--model",
              "ollama/llama3.1:8b@v1"])
        == 2
    )
    capsys.readouterr()

    # 2 — invalid enum/usage
    assert (
        main(["--db", database, "memory", "add", "Bad WS", "t"]) == 2
    )
    capsys.readouterr()
