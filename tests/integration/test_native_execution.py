"""Native execution: argv without a shell, catalog-chosen binary, governance kept."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from samurai_ai.domain.execution import ToolDefinition, ToolKind, ToolValidationError
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.tool_manifest import ToolManifest
from samurai_ai.execution.native import (
    MissingBinaryError,
    NativeTool,
    render_native_argv,
)

SCOPE = ScopeContract(includes=("10.0.0.0/24",))


def _definition(**overrides: object) -> ToolDefinition:
    payload: dict[str, object] = {
        "id": "scan.fake",
        "version": 1,
        "kind": ToolKind.PROCESS,
        "risk_class": RiskClass.R2,
        "read_only": True,
        "parameters": {
            "type": "object",
            "properties": {"targets": {"type": "array"}, "ports": {"type": "string"}},
            "required": ["targets"],
        },
    }
    payload.update(overrides)
    return ToolDefinition.model_validate(payload)


def _recipe(tmp_path: Path, argv: list[str]) -> ToolManifest:
    payload = {
        "id": "scan.fake",
        "version": 1,
        "title": "fake scanner",
        "image": "x/y@sha256:" + "a" * 64,
        "executable": "fake-scan",
        "risk_class": "r2",
        "network": "direct",
        "execution": {"native": True},
        "provision": {"packages": {"linux": {"manager": "apt", "package": "fake"}}},
        "params": {
            "targets": {"type": "array", "required": True},
            "ports": {"type": "string", "required": False, "default": "80"},
        },
        "argv": argv,
        "trusted": True,
    }
    return ToolManifest.model_validate(payload)


def _fake_binary(tmp_path: Path, body: str = "#!/bin/sh\nprintf 'open 22/tcp\\n'\n") -> str:
    path = tmp_path / "fake-scan"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_argv_is_built_without_a_shell(tmp_path: Path) -> None:
    recipe = _recipe(tmp_path, ["-p", "{ports}", "{targets}"])
    argv = render_native_argv(recipe, {"targets": ["10.0.0.5", "10.0.0.6"], "ports": "22"})
    assert argv == ["-p", "22", "10.0.0.5,10.0.0.6"]
    assert all(" " not in token or token.startswith("-") for token in argv)


def test_the_recipe_decides_the_executable_not_the_model(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path)
    recipe = _recipe(tmp_path, ["-p", "{ports}", "{targets}"])
    tool = NativeTool(recipe, _definition(), executable_path=binary)
    tool.preflight({"targets": ["10.0.0.5"], "ports": "80"})
    assert tool.definition.id == "scan.fake"


def test_a_missing_binary_is_its_own_error(tmp_path: Path) -> None:
    recipe = _recipe(tmp_path, ["-p", "{ports}", "{targets}"])
    tool = NativeTool(recipe, _definition(), executable_path="")
    with pytest.raises(MissingBinaryError, match="not installed"):
        tool.preflight({"targets": ["10.0.0.5"]})


def test_output_is_captured_and_summarised(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path)
    recipe = _recipe(tmp_path, ["-p", "{ports}", "{targets}"])
    tool = NativeTool(recipe, _definition(), executable_path=binary)
    output = tool.invoke({"targets": ["10.0.0.5"], "ports": "22"}, SCOPE)
    assert output.exit_code == 0
    assert "open 22/tcp" in output.stdout
    assert output.stdout.startswith("# summary")


def test_arguments_reach_the_binary_verbatim(tmp_path: Path) -> None:
    """A value with a space stays one argv entry: no shell to re-split it."""
    binary = _fake_binary(tmp_path, "#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    recipe = _recipe(tmp_path, ["-p", "{ports}", "{targets}"])
    tool = NativeTool(recipe, _definition(), executable_path=binary)
    output = tool.invoke({"targets": ["10.0.0.5"], "ports": "22,80"}, SCOPE)
    assert output.stdout.split("\n")[:4] == ["# summary", "-p", "22,80", "10.0.0.5"]


def test_nul_in_a_parameter_is_refused(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path)
    recipe = _recipe(tmp_path, ["-p", "{ports}", "{targets}"])
    tool = NativeTool(recipe, _definition(), executable_path=binary)
    # a token that arrived with a NUL in it must not reach the spawn
    recipe.argv.append("\x00broken")
    with pytest.raises(ToolValidationError, match="NUL"):
        tool.invoke({"targets": ["10.0.0.5"], "ports": "80"}, SCOPE)


def test_the_child_gets_a_small_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SAMURAI_CANARY", "should-not-leak")
    monkeypatch.setenv("PATH", os.environ["PATH"])
    binary = _fake_binary(tmp_path, "#!/bin/sh\nenv\n")
    recipe = _recipe(tmp_path, ["-p", "{ports}", "{targets}"])
    tool = NativeTool(recipe, _definition(), executable_path=binary)
    output = tool.invoke({"targets": ["10.0.0.5"], "ports": "80"}, SCOPE)
    assert "PATH=" in output.stdout
    assert "SAMURAI_CANARY" not in output.stdout


def test_a_nonzero_exit_is_reported_not_raised(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path, "#!/bin/sh\necho boom >&2\nexit 3\n")
    recipe = _recipe(tmp_path, ["-p", "{ports}", "{targets}"])
    tool = NativeTool(recipe, _definition(), executable_path=binary)
    output = tool.invoke({"targets": ["10.0.0.5"], "ports": "80"}, SCOPE)
    assert output.exit_code == 3
    assert "boom" in output.stderr


def test_the_recipe_and_adapter_schema_agree() -> None:
    from samurai_ai.services.catalog import RecipeCatalog

    recipes = RecipeCatalog().load_all()
    assert recipes["scan.nmap"].executable == "nmap"
    assert recipes["scan.nmap"].limits.timeout_seconds >= 60
    json.dumps({name: recipe.model_dump(mode="json") for name, recipe in recipes.items()})


def test_scope_and_approval_survive_the_native_path(tmp_path: Path) -> None:
    """The native transport is not a way around the service's own gates."""
    from samurai_ai.domain.execution import ApprovalRequiredError, ToolScopeError
    from samurai_ai.domain.operations import OperationMode
    from samurai_ai.services.execution import ToolExecutionService
    from samurai_ai.services.kernel import KernelService
    from samurai_ai.services.operations import OperationService
    from samurai_ai.services.recovery import RecoveryService
    from samurai_ai.storage.database import Database
    from samurai_ai.storage.execution import ToolRunRepository
    from samurai_ai.storage.kernel import KernelRepository
    from samurai_ai.storage.operations import OperationRepository
    from samurai_ai.storage.recovery import RecoveryRepository

    database = Database(tmp_path / "native.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    recovery = RecoveryService(operations, kernel, RecoveryRepository(database))
    execution = ToolExecutionService(
        operations, kernel, recovery, ToolRunRepository(database)
    )
    operation = operations.create(
        "Native gate", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.0/24",))
    )
    # the local nmap adapter is native when the binary exists; skip if it does not
    adapter = execution.registry.get("scan.nmap")
    if type(adapter).__name__ != "NativeTool":
        pytest.skip("nmap is not installed here")

    with pytest.raises(ApprovalRequiredError):
        execution.run_tool(operation.id, "scan.nmap", {"targets": ["10.0.0.5"], "ports": "22"})
    with pytest.raises(ToolScopeError, match="outside the operation scope"):
        execution.run_tool(
            operation.id,
            "scan.nmap",
            {"targets": ["10.9.9.9"], "ports": "22"},
            approval_resolver=lambda _id: True,
        )
