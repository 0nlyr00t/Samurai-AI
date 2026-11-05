"""The relay flag reaches the operation, and the executor swaps the runner."""

import argparse
from pathlib import Path
from typing import Any

import samurai_ai.cli  # noqa: F401 - the service graph imports in this order
from samurai_ai.cli_swarm import register_swarm_commands
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.relay import RelaySandboxRunner
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    register_swarm_commands(subcommands)
    return parser.parse_args(argv)


def service(tmp_path: Path) -> OperationService:
    return OperationService(OperationRepository(Database(tmp_path / "relay.db")))


def make(service: OperationService) -> str:
    return service.create(
        "Relay lab", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.1",))
    ).id


def test_the_flag_exists_on_swarm_run() -> None:
    args = parse(["swarm", "run", "op_1", "--model", "m", "--relay", "ssh -J jump"])
    assert args.relay == "ssh -J jump"


def test_setting_and_clearing_the_relay_is_an_evented_change(tmp_path: Path) -> None:
    operations = service(tmp_path)
    operation_id = make(operations)
    updated = operations.set_relay(operation_id, "  prlctl exec '{uuid}'  ")
    assert updated.relay == "prlctl exec '{uuid}'"
    events = [event.event_type for event in operations.events(operation_id)]
    assert "operation.relay.changed" in events
    assert operations.set_relay(operation_id, "").relay == ""


class _Adapter:
    _runner: Any = None

    def __init__(self) -> None:
        self.runner = _Adapter._runner


def test_the_executor_swaps_in_a_relay_runner_and_back() -> None:
    from samurai_ai.services.execution import ToolExecutionService

    adapter = _Adapter()
    operation = type("Op", (), {"relay": "ssh -J jump"})()
    ToolExecutionService._apply_relay(None, operation, adapter)  # type: ignore[arg-type]
    assert isinstance(adapter._runner, RelaySandboxRunner)

    operation.relay = ""
    ToolExecutionService._apply_relay(None, operation, adapter)  # type: ignore[arg-type]
    assert adapter._runner is None
