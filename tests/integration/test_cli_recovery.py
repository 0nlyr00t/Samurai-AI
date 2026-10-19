import json
from pathlib import Path

from samurai_ai.cli import main
from samurai_ai.domain.execution import ToolRun
from samurai_ai.domain.kernel import PlanTask
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository


def create_operation(database: str, capsys, name: str = "Recovery CLI") -> str:
    result = main(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            name,
            "--mode",
            "bug-bounty",
            "--include",
            "*.example.com",
        ]
    )
    assert result == 0
    return json.loads(capsys.readouterr().out)["id"]


def write_plan(tmp_path: Path, title: str, task_ids: list[str]) -> Path:
    plan_file = tmp_path / f"plan-{title}.json"
    plan_file.write_text(
        json.dumps(
            {
                "title": title,
                "tasks": [{"id": task_id, "title": task_id} for task_id in task_ids],
                "gates": [{"id": "gate-one", "name": "Gate one"}],
            }
        ),
        encoding="utf-8",
    )
    return plan_file


def make_services(database: str):
    db = Database(database)
    service = OperationService(OperationRepository(db))
    kernel = KernelService(service, KernelRepository(db))
    recovery = RecoveryService(service, kernel, RecoveryRepository(db))
    return service, kernel, recovery


def test_cli_snapshot_lifecycle(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "snapshots.db")
    operation_id = create_operation(database, capsys)

    assert main(["--db", database, "--json", "snapshot", "create", operation_id]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["operation_id"] == operation_id
    assert snapshot["label"] == ""

    assert main(
        [
            "--db",
            database,
            "--json",
            "snapshot",
            "create",
            operation_id,
            "--label",
            "baseline",
        ]
    ) == 0
    labeled = json.loads(capsys.readouterr().out)

    assert main(["--db", database, "--json", "snapshot", "list", operation_id]) == 0
    snapshots = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in snapshots] == [snapshot["id"], labeled["id"]]

    assert main(["--db", database, "--json", "snapshot", "show", labeled["id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["label"] == "baseline"
    assert shown["data"]["operation"]["id"] == operation_id

    assert main(
        [
            "--db",
            database,
            "--json",
            "snapshot",
            "restore",
            labeled["id"],
            "--name",
            "Restored lab",
        ]
    ) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["name"] == "Restored lab"
    assert restored["id"] != operation_id
    assert restored["status"] == "draft"

    assert main(["--db", database, "--json", "operation", "list"]) == 0
    operations = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in operations} == {operation_id, restored["id"]}


def test_cli_operation_fork_resets_plan_progress(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "fork.db")
    operation_id = create_operation(database, capsys)
    plan_file = write_plan(tmp_path, "v1", ["recon", "enum"])
    assert main(["--db", database, "plan", "create", operation_id, str(plan_file)]) == 0
    capsys.readouterr()
    assert main(["--db", database, "task", "start", operation_id, "recon"]) == 0
    capsys.readouterr()

    assert main(["--db", database, "--json", "operation", "fork", operation_id]) == 0
    forked = json.loads(capsys.readouterr().out)
    assert forked["id"] != operation_id
    assert forked["name"].endswith("(fork)")

    assert main(["--db", database, "--json", "plan", "show", forked["id"]]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert not plan["approved"]
    assert {task["id"]: task["status"] for task in plan["tasks"]} == {
        "recon": "pending",
        "enum": "pending",
    }

    service, kernel, _ = make_services(database)
    event_types = [event.event_type for event in service.events(forked["id"])]
    assert event_types == ["operation.forked", "plan.forked"]
    assert kernel.kernel.list_findings(forked["id"]) == []
    assert kernel.kernel.list_artifacts(forked["id"]) == []
    assert kernel.kernel.list_approvals(forked["id"]) == []


def test_cli_reconcile_marks_interrupted_actions(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "reconcile.db")
    operation_id = create_operation(database, capsys)
    _, _, recovery = make_services(database)
    action = recovery.begin_action(operation_id, "agent-step")

    assert main(["--db", database, "--json", "reconcile", operation_id]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["actions_marked_unknown"] == [action.id]
    assert report["resumable_tasks"] == []
    assert report["pending_approvals"] == []

    assert main(["--db", database, "reconcile", operation_id]) == 0
    output = capsys.readouterr().out
    assert "actions-marked-unknown: -" in output

    _, _, reopened = make_services(database)
    stored = reopened.recovery.get_action(action.id)
    assert stored is not None
    assert stored.status.value == "unknown"


def test_cli_plan_diff(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "diff.db")
    operation_id = create_operation(database, capsys)
    _, kernel, _ = make_services(database)
    old_plan = kernel.create_plan(
        operation_id,
        "v1",
        [PlanTask(id="recon", title="Recon")],
        [],
    )
    new_plan = kernel.revise_plan(
        operation_id,
        "v2",
        [PlanTask(id="enum", title="Enum")],
        [],
    )
    capsys.readouterr()

    assert main(
        ["--db", database, "--json", "plan", "diff", operation_id, old_plan.id, new_plan.id]
    ) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["tasks"]["added"] == ["enum"]
    assert diff["tasks"]["removed"] == ["recon"]
    assert diff["tasks"]["changed"] == []

    assert main(
        ["--db", database, "plan", "diff", operation_id, old_plan.id, "plan_missing"]
    ) == 2
    assert "plan not found" in capsys.readouterr().err


def test_cli_lang_set_and_show(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("SAMURAI_LANG", raising=False)
    database = str(tmp_path / "lang.db")

    assert main(["--db", database, "--json", "lang"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown == {"persisted": None, "effective": "en"}

    assert main(["--db", database, "--json", "lang", "ru"]) == 0
    assert json.loads(capsys.readouterr().out) == {"persisted": "ru", "effective": "ru"}

    assert main(["--db", database, "lang"]) == 0
    output = capsys.readouterr().out
    assert "persisted: ru" in output
    assert "effective: ru" in output

    assert main(["--db", database, "lang", "de"]) == 2
    assert "unsupported locale" in capsys.readouterr().err


def test_cli_reconcile_marks_tool_runs_failed(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "reconcile-tool-runs.db")
    operation_id = create_operation(database, capsys)
    runs = ToolRunRepository(Database(database))
    run = ToolRun(operation_id=operation_id, tool_id="local.echo", tool_version=1)
    runs.add_run(
        run,
        OperationEvent(
            operation_id=operation_id,
            event_type="tool.run.queued",
            payload={"run_id": run.id},
        ),
    )
    expected_version = run.version
    run.start()
    runs.save_run(
        run,
        expected_version,
        OperationEvent(
            operation_id=operation_id,
            event_type="tool.run.started",
            payload={"run_id": run.id},
        ),
    )

    result = main(["--db", database, "--json", "reconcile", operation_id])
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tool_runs_marked_failed"] == [run.id]
    restored = runs.get_run(run.id)
    assert restored is not None
    assert restored.status.value == "failed"
