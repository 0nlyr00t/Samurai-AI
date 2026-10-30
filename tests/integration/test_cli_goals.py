import json

from samurai_ai.cli import main
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository


def make_operation(database: str) -> str:
    operation = Operation(
        name="Goal CLI op",
        mode=OperationMode.RED_TEAM,
        scope=ScopeContract(includes=("example.com",)),
    )
    OperationRepository(Database(database)).add(operation, _created(operation))
    return operation.id


def _created(operation: Operation):  # type: ignore[no-untyped-def]
    from samurai_ai.domain.operations import OperationEvent

    return OperationEvent(
        operation_id=operation.id,
        event_type="operation.created",
        payload={"name": operation.name},
    )


def test_goal_create_status_and_pause(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = str(tmp_path / "goal.db")
    operation_id = make_operation(database)

    assert (
        main(
            [
                "--db",
                database,
                "--json",
                "goal",
                "create",
                operation_id,
                "Get DA on DC01",
                "--finish-line",
                "da.txt exists",
                "--budget-steps",
                "5",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["state"] == "active"
    assert created["budget_steps"] == 5
    assert created["finish_line"] == "da.txt exists"

    assert main(["--db", database, "--json", "goal", "status", operation_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == created["id"]

    assert main(["--db", database, "--json", "goal", "pause", operation_id]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["state"] == "paused"

    assert main(["--db", database, "--json", "goal", "resume", operation_id]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "active"


def test_goal_step_blocks_on_budget(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = str(tmp_path / "goal.db")
    operation_id = make_operation(database)
    main(
        [
            "--db",
            database,
            "--json",
            "goal",
            "create",
            operation_id,
            "probe",
            "--budget-steps",
            "1",
        ]
    )
    capsys.readouterr()

    assert main(["--db", database, "--json", "goal", "step", operation_id]) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["state"] == "blocked"
    assert "step budget 1 spent" in blocked["block_reason"]


def test_goal_next_queues_and_complete_promotes(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = str(tmp_path / "goal.db")
    operation_id = make_operation(database)
    main(["--db", database, "--json", "goal", "create", operation_id, "first"])
    first = json.loads(capsys.readouterr().out)
    main(["--db", database, "--json", "goal", "create", operation_id, "second", "--next"])
    second = json.loads(capsys.readouterr().out)
    assert second["state"] == "queued"

    assert main(["--db", database, "--json", "goal", "complete", operation_id]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "complete"

    assert main(["--db", database, "--json", "goal", "status", operation_id]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["objective"] == "second"
    assert first["id"] not in out


def test_goal_replace_and_cancel(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = str(tmp_path / "goal.db")
    operation_id = make_operation(database)
    main(["--db", database, "--json", "goal", "create", operation_id, "first"])
    first = json.loads(capsys.readouterr().out)

    main(["--db", database, "--json", "goal", "replace", operation_id, "second"])
    replaced = json.loads(capsys.readouterr().out)
    assert replaced["objective"] == "second"

    assert main(["--db", database, "--json", "goal", "cancel", operation_id]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "cancelled"
    assert first["state"] == "active"  # the snapshot from before the replace


def test_goal_without_a_current_goal_fails_cleanly(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = str(tmp_path / "goal.db")
    operation_id = make_operation(database)

    assert main(["--db", database, "goal", "pause", operation_id]) == 2
    captured = capsys.readouterr()
    assert "no current goal" in captured.err


def test_goal_status_prints_plain_text_without_json(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = str(tmp_path / "goal.db")
    operation_id = make_operation(database)
    main(["--db", database, "goal", "status", operation_id])
    assert "no current goal" in capsys.readouterr().out

    main(["--db", database, "goal", "create", operation_id, "plain"])
    capsys.readouterr()
    assert main(["--db", database, "goal", "status", operation_id]) == 0
    out = capsys.readouterr().out
    assert "active" in out
    assert "plain" in out
