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


def test_the_executor_prompt_announces_the_relay() -> None:
    """Without the line the model works around the host instead of using tools."""
    from samurai_ai.agents.orchestrator import AgentOrchestrator

    class Ops:
        def __init__(self, relay: str) -> None:
            self.relay = relay

        def get(self, _id: str) -> object:
            return type("Op", (), {"relay": self.relay})()

    stand = object.__new__(AgentOrchestrator)
    stand.operations = Ops("prlctl exec '{uuid}'")  # type: ignore[assignment]
    notice = stand._relay_notice("op_1")  # type: ignore[attr-defined]
    assert "already go through it" in notice
    assert "NO route to the target" in notice
    assert "127.0.0.1 under a relay is the VM itself" in notice

    stand.operations = Ops("")  # type: ignore[assignment]
    assert stand._relay_notice("op_1") == ""  # type: ignore[attr-defined]


def test_an_http_fetch_takes_the_relay_prefix() -> None:
    """http.fetch is the tool the agent reaches for first: it must relay too."""
    from samurai_ai.execution.registry import HttpFetchTool

    tool = HttpFetchTool()
    assert tool.relay_supported is True
    operation = type("Op", (), {"relay": "prlctl exec '{uuid}'"})()
    from samurai_ai.services.execution import ToolExecutionService

    ToolExecutionService._apply_relay(None, operation, tool)  # type: ignore[arg-type]
    assert tool.relay == "prlctl exec '{uuid}'"


def test_the_scope_line_is_always_there_and_names_the_targets() -> None:
    from samurai_ai.agents.orchestrator import AgentOrchestrator, _rejection_block
    from samurai_ai.domain.scope import ScopeContract

    class Ops:
        def get(self, _id: str) -> object:
            return type(
                "Op", (), {"scope": ScopeContract(includes=("10.0.0.5",), excludes=("10.0.0.6",))}
            )()

    stand = object.__new__(AgentOrchestrator)
    stand.operations = Ops()  # type: ignore[assignment]
    notice = stand._scope_notice("op_1")  # type: ignore[attr-defined]
    assert "in-scope targets: 10.0.0.5" in notice
    assert "excluded: 10.0.0.6" in notice

    block = _rejection_block(["'127.0.0.1' is outside the operation scope"], "10.0.0.5")
    assert "Only these targets are allowed: 10.0.0.5" in block


def test_an_exploit_run_takes_the_relay_and_skips_the_tunnel() -> None:
    """A PoC in a container is blind under a relay; it runs on the far side instead."""
    import asyncio

    from samurai_ai.domain.scope import ScopeContract
    from samurai_ai.execution.exploits import ExploitRunTool

    tool = ExploitRunTool()
    assert tool.relay_supported is True
    operation = type("Op", (), {"relay": "prlctl exec '{uuid}'"})()
    from samurai_ai.services.execution import ToolExecutionService

    ToolExecutionService._apply_relay(None, operation, tool)  # type: ignore[arg-type]
    assert tool.relay == "prlctl exec '{uuid}'"

    seen: dict[str, object] = {}

    class _Manager:
        stall_seconds = 120.0

        async def run(self, argv, timeout_seconds=0, **_k):  # noqa: ANN001, ANN003
            seen["argv"] = argv
            return type(
                "R", (), {"stdout": "poc ok", "stderr": "", "exit_code": 0,
                          "timed_out": False, "truncated": False}
            )()

    class _Tool(ExploitRunTool):
        async def _manager(self):  # noqa: D102 - replaced below
            raise NotImplementedError

    import samurai_ai.execution.process as process_module

    original = process_module.ProcessManager
    process_module.ProcessManager = lambda **_k: _Manager()  # type: ignore[assignment]
    try:
        output = asyncio.run(
            tool._run_relayed("print(1)", "10.0.0.1", 80)
        )
    finally:
        process_module.ProcessManager = original  # type: ignore[assignment]
    assert output.stdout == "poc ok"
    command = seen["argv"]
    assert isinstance(command, list) and command[0] == "bash"
    assert "10.0.0.1:80" in command[2]
    assert ScopeContract is not None


def test_a_relayed_read_falls_back_to_a_plain_fetch() -> None:
    """No browser image on the far side: curl plus stripping still gives a read."""
    from samurai_ai.domain.scope import ScopeContract
    from samurai_ai.execution import process as process_module
    from samurai_ai.execution import relay as relay_module
    from samurai_ai.execution.security import ResearchReadTool

    tool = ResearchReadTool()
    tool.relay = "prlctl exec '{uuid}'"
    assert tool.relay_supported is True

    class _Manager:
        stall_seconds = 120.0

        async def run(self, *_a, **_k):  # noqa: ANN002, ANN003
            body = "<title>Dash</title><a href='/x'>go</a><p>hello world</p>"
            return type(
                "R", (), {"stdout": f"HTTP/1.1 200 OK\r\n\r\n{body}", "stderr": "",
                          "exit_code": 0, "timed_out": False}
            )()

    original = process_module.ProcessManager
    process_module.ProcessManager = lambda **_k: _Manager()  # type: ignore[assignment]
    try:
        output = tool.invoke(
            {"url": "http://10.0.0.1/", "max_chars": 500},
            ScopeContract(includes=("10.0.0.1",)),
        )
    finally:
        process_module.ProcessManager = original  # type: ignore[assignment]

    import json

    payload = json.loads(output.stdout)
    assert payload["status"] == 200
    assert payload["title"] == "Dash"
    assert "hello world" in payload["text"]
    assert "relayed, no browser" in output.stderr
    assert relay_module is not None


def test_the_relay_host_lands_in_the_scope(tmp_path: Path) -> None:
    from samurai_ai.cli_run import _allow_relay_host
    from samurai_ai.domain.operations import OperationMode
    from samurai_ai.domain.scope import ScopeContract
    from samurai_ai.execution import relay as relay_module

    operations = service(tmp_path)
    operation_id = operations.create(
        "Relay host", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.1",))
    ).id
    original = relay_module.relay_host
    relay_module.relay_host = lambda _relay: "10.211.55.27"  # type: ignore[assignment]
    try:
        _allow_relay_host("relay", operation_id, operations)
    finally:
        relay_module.relay_host = original  # type: ignore[assignment]
    assert "10.211.55.27" in operations.get(operation_id).scope.includes


def test_no_address_means_no_scope_change(tmp_path: Path) -> None:
    from samurai_ai.cli_run import _allow_relay_host
    from samurai_ai.domain.operations import OperationMode
    from samurai_ai.domain.scope import ScopeContract
    from samurai_ai.execution import relay as relay_module

    operations = service(tmp_path)
    operation_id = operations.create(
        "No host", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.1",))
    ).id
    original = relay_module.relay_host
    relay_module.relay_host = lambda _relay: ""  # type: ignore[assignment]
    try:
        _allow_relay_host("relay", operation_id, operations)
    finally:
        relay_module.relay_host = original  # type: ignore[assignment]
    assert operations.get(operation_id).scope.includes == ("10.0.0.1",)
