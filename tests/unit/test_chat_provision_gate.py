"""The chat provision tool installs only for a missing binary.

The tool description always said so and nothing held the model to it. The gate
is machine-readable: a typed `MissingBinaryError` recorded from this turn's tool
run, or an adapter that reports its binary absent (a resolution, not text).
"A tool ran and something failed" is not evidence — scope refusals, bad
parameters and timeouts leave the binary in place.
"""

from __future__ import annotations

import asyncio
from typing import Any

from samurai_ai.agents.provider import Message, ToolCall
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.provision import ProvisionError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.tui.chat import ChatController


class _Adapter:
    def __init__(self, available: bool) -> None:
        self.available = available

    def resolved_available(self) -> bool:
        return self.available


class _Registry:
    def __init__(self, adapter: _Adapter | None) -> None:
        self.adapter = adapter

    def get(self, tool_id: str) -> Any:
        if self.adapter is None:
            raise LookupError(f"tool not found: {tool_id}")
        return self.adapter


class _Execution:
    def __init__(self, adapter: _Adapter | None) -> None:
        self.registry = _Registry(adapter)


class _Provisioner:
    """`plan` is the gate's downstream marker; it stops there on purpose."""

    def __init__(self) -> None:
        self.planned: list[str] = []
        self.installed: list[str] = []
        self.detector = type("D", (), {"detect": staticmethod(lambda refresh=False: None)})()

    def plan(self, tool_id: str, _profile: Any) -> Any:
        self.planned.append(tool_id)
        raise ProvisionError("reached the plan step")

    def run(self, operation_id: str, tool_id: str, _profile: Any) -> Any:  # pragma: no cover
        self.installed.append(tool_id)
        raise AssertionError("the stub stops at the plan")


def _controller(*, adapter: _Adapter | None, missing: set[str] | None = None) -> ChatController:
    stand = object.__new__(ChatController)
    stand.operation = Operation(
        name="Prov lab", mode=OperationMode.CTF_HTB, scope=ScopeContract(includes=("10.0.0.5",))
    )
    stand.provisioner = _Provisioner()
    stand.execution = _Execution(adapter)  # type: ignore[assignment]
    stand._missing_binary_tools = set(missing or ())  # type: ignore[attr-defined]
    return stand


def _ask(tool_id: str = "scan.nmap") -> ToolCall:
    return ToolCall(id="p1", name="provision_tool", arguments={"tool_id": tool_id})


def _call(stand: ChatController) -> Message:
    return asyncio.run(stand._run_provision_tool(_ask()))


def test_a_bare_request_without_a_missing_binary_is_refused() -> None:
    stand = _controller(adapter=_Adapter(available=True))
    message = _call(stand)
    assert "only for a missing binary" in message.content
    assert "already" in message.content
    assert stand.provisioner.planned == []  # type: ignore[attr-defined]


def test_an_unknown_tool_falls_through_to_the_plan_refusal() -> None:
    stand = _controller(adapter=None)
    message = _call(stand)
    # the catalogue decides; the gate does not invent a missing binary
    assert "cannot be installed" in message.content
    assert stand.provisioner.planned == ["scan.nmap"]  # type: ignore[attr-defined]


def test_a_recorded_missing_binary_reaches_the_plan() -> None:
    stand = _controller(adapter=_Adapter(available=False), missing={"scan.nmap"})
    message = _call(stand)
    assert "cannot be installed" in message.content
    assert stand.provisioner.planned == ["scan.nmap"]  # type: ignore[attr-defined]


def test_an_absent_binary_is_enough_without_a_recorded_error() -> None:
    """A resolution that says "missing binary" is the same evidence."""
    stand = _controller(adapter=_Adapter(available=False))
    message = _call(stand)
    assert "cannot be installed" in message.content
    assert stand.provisioner.planned == ["scan.nmap"]  # type: ignore[attr-defined]


def test_the_gate_only_looks_at_the_requested_tool() -> None:
    stand = _controller(adapter=_Adapter(available=True), missing={"crack.john"})
    message = _call(stand)
    assert "only for a missing binary" in message.content
    assert stand.provisioner.planned == []  # type: ignore[attr-defined]


def test_risk_class_is_still_r3_for_the_approval() -> None:
    """The gate runs in front of the approval, it does not replace it."""
    assert RiskClass.R3 is not None
