"""The short paths: scope rules, plan from flags, tasks by title, approve all."""

import json
from pathlib import Path

from samurai_ai.cli import main


def make_operation(tmp_path: Path, capsys) -> tuple[str, str]:
    database = str(tmp_path / "ergo.db")
    assert main(["--db", database, "--json", "operation", "create", "Ergo",
                 "--mode", "red-team", "--include", "127.0.0.1"]) == 0
    return database, json.loads(capsys.readouterr().out)["id"]


def test_scope_rules_are_bare_arguments(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "scope", "add", operation_id, "localhost"]) == 0
    assert "127.0.0.1 localhost" in capsys.readouterr().out
    assert main(["--db", database, "scope", "rm", operation_id, "localhost"]) == 0
    assert "127.0.0.1" in capsys.readouterr().out
    # removing what is not there is an error, not a silent no-op
    assert main(["--db", database, "scope", "rm", operation_id, "nope"]) == 1


def test_a_plan_can_be_typed_without_a_file(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "plan", "create", operation_id,
                 "--title", "Takeoff", "--task", "Scan", "--task", "Report"]) == 0
    assert "step2" in capsys.readouterr().out
    # file and flags together are ambiguous
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({"title": "T", "tasks": []}))
    try:
        main(["--db", database, "plan", "create", operation_id, str(plan_file), "--task", "x"])
    except ValueError as exc:
        assert "not both" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("file and flags were both accepted")


def test_tasks_resolve_by_title(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "plan", "create", operation_id, "--task", "Scan ports",
                 "--task", "Report"]) == 0
    assert main(["--db", database, "plan", "approve", operation_id]) == 0
    capsys.readouterr()
    assert main(["--db", database, "task", "start", operation_id, "scan"]) == 0
    assert "in-progress" in capsys.readouterr().out
    # no artifact yet: the evidence gate refuses, --force closes anyway
    assert main(["--db", database, "task", "complete", operation_id, "SCAN PORTS"]) == 2
    assert "no evidence" in capsys.readouterr().err
    assert (
        main(["--db", database, "task", "complete", operation_id, "SCAN PORTS", "--force"]) == 0
    )
    try:
        main(["--db", database, "task", "start", operation_id, "zzz"])
    except ValueError as exc:
        assert "no task matches" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown task started")


def test_approve_all_clears_the_pending_queue(tmp_path: Path, capsys) -> None:
    database, operation_id = make_operation(tmp_path, capsys)
    assert main(["--db", database, "approval", "request", operation_id,
                 "--action", "model.call x/y", "--risk", "r3"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "approval", "approve", "--all",
                 "--operation", operation_id]) == 0
    assert "approved" in capsys.readouterr().out
    assert main(["--db", database, "approval", "approve", "--all",
                 "--operation", operation_id]) == 0
    assert "nothing is pending" in capsys.readouterr().out


def test_run_reports_an_unregistered_model_cleanly(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "run.db")
    assert main(["--db", database, "run", "Box", "--mode", "bug-bounty",
                 "--include", "10.10.10.10", "--task", "Scan", "--no-preflight",
                 "--model", "ghost/model@v1"]) == 2
    assert "ghost/model@v1" in capsys.readouterr().err


def test_run_without_tasks_is_a_usage_error(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "run.db")
    assert main(["--db", database, "run", "Box", "--mode", "bug-bounty",
                 "--include", "10.10.10.10", "--no-preflight",
                 "--model", "missing/model@v1"]) == 2
    assert "missing/model@v1" in capsys.readouterr().err
