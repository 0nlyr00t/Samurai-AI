"""Versioned, fail-closed tool registry with the built-in Stage 2a adapters.

Tools are static, code-defined adapters — there is no API to register
arbitrary command strings or dynamic callables at runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from samurai_ai.config import sandbox_allowlist_path, vault_key_path
from samurai_ai.domain.execution import (
    ToolDefinition,
    ToolKind,
    ToolNotFoundError,
    ToolValidationError,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.http import HttpEgressBroker
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.sandbox import DockerSandboxRunner, get_profile


@dataclass(frozen=True)
class ToolOutput:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


class ToolAdapter(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput: ...


class ToolRegistry:
    def __init__(self, adapters: Iterable[ToolAdapter] = ()) -> None:
        self._adapters: dict[tuple[str, int], ToolAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ToolAdapter) -> None:
        definition = adapter.definition
        self._validate_definition(definition)
        key = (definition.id, definition.version)
        if key in self._adapters:
            raise ToolValidationError(
                f"duplicate tool registration: {definition.id} v{definition.version}"
            )
        self._adapters[key] = adapter

    def get(self, tool_id: str, version: int | None = None) -> ToolAdapter:
        versions = sorted(v for (registered_id, v) in self._adapters if registered_id == tool_id)
        if not versions:
            raise ToolNotFoundError(f"tool not found: {tool_id}")
        resolved = version if version is not None else versions[-1]
        adapter = self._adapters.get((tool_id, resolved))
        if adapter is None:
            raise ToolNotFoundError(f"tool not found: {tool_id} v{resolved}")
        return adapter

    def definitions(self) -> list[ToolDefinition]:
        return [
            adapter.definition
            for _, adapter in sorted(self._adapters.items(), key=lambda item: item[0])
        ]

    def validate_params(
        self, definition: ToolDefinition, params: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ToolValidationError(f"params for {definition.id} must be an object")
        schema = definition.parameters
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", False)

        unknown = sorted(key for key in params if key not in properties)
        if unknown and not additional:
            raise ToolValidationError(
                f"unknown parameters for {definition.id}: {', '.join(unknown)}"
            )
        validated: dict[str, Any] = {}
        for name, spec in properties.items():
            if name in params:
                validated[name] = _validate_value(definition.id, name, params[name], spec)
            elif isinstance(spec, dict) and "default" in spec:
                validated[name] = spec["default"]
        missing = [name for name in required if name not in validated]
        if missing:
            raise ToolValidationError(
                f"missing required parameters for {definition.id}: {', '.join(missing)}"
            )
        for name in unknown:
            validated[name] = params[name]
        return validated

    @staticmethod
    def _validate_definition(definition: ToolDefinition) -> None:
        schema = definition.parameters
        if not isinstance(schema, dict):
            raise ToolValidationError(f"parameters schema for {definition.id} must be an object")
        if schema and schema.get("type", "object") != "object":
            raise ToolValidationError(
                f"parameters schema for {definition.id} must describe an object"
            )
        if "properties" in schema and not isinstance(schema["properties"], dict):
            raise ToolValidationError(
                f"parameters properties for {definition.id} must be an object"
            )
        if definition.kind == ToolKind.HTTP and not definition.network_access:
            raise ToolValidationError(
                f"http tool {definition.id} must declare network_access=True"
            )
        if definition.kind != ToolKind.HTTP and definition.network_access:
            raise ToolValidationError(
                f"non-http tool {definition.id} cannot declare network_access=True"
            )


def _validate_value(tool_id: str, name: str, value: Any, spec: Any) -> Any:
    if not isinstance(spec, dict):
        return value
    expected = spec.get("type")
    label = f"{tool_id} parameter {name!r}"
    if expected == "string":
        if not isinstance(value, str):
            raise ToolValidationError(f"{label} must be a string")
        if "minLength" in spec and len(value) < int(spec["minLength"]):
            raise ToolValidationError(f"{label} is shorter than {spec['minLength']}")
        if "maxLength" in spec and len(value) > int(spec["maxLength"]):
            raise ToolValidationError(f"{label} is longer than {spec['maxLength']}")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolValidationError(f"{label} must be an integer")
    elif expected == "number":
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ToolValidationError(f"{label} must be a number")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ToolValidationError(f"{label} must be a boolean")
    elif expected == "array":
        if not isinstance(value, list):
            raise ToolValidationError(f"{label} must be an array")
        if "minItems" in spec and len(value) < int(spec["minItems"]):
            raise ToolValidationError(f"{label} needs at least {spec['minItems']} items")
        item_spec = spec.get("items")
        if isinstance(item_spec, dict):
            for index, item in enumerate(value):
                _validate_value(tool_id, f"{name}[{index}]", item, item_spec)
    elif expected == "object":
        if not isinstance(value, dict):
            raise ToolValidationError(f"{label} must be an object")
    if "enum" in spec and value not in spec["enum"]:
        raise ToolValidationError(f"{label} must be one of {spec['enum']}")
    return value


class LocalEchoTool:
    def __init__(self) -> None:
        self._definition = ToolDefinition(
            id="local.echo",
            version=1,
            kind=ToolKind.LOCAL,
            description="Echo a short text back to the operator",
            risk_class=RiskClass.R0,
            network_access=False,
            timeout_seconds=5,
            output_limit_bytes=4096,
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 4096}},
                "required": ["text"],
                "additionalProperties": False,
            },
            capabilities=("echo",),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        text = params["text"]
        encoded = text.encode("utf-8")[: self._definition.output_limit_bytes]
        return ToolOutput(stdout=encoded.decode("utf-8", errors="ignore"), stderr="", exit_code=0)


class ProcessExecTool:
    def __init__(self, manager: ProcessManager | None = None) -> None:
        self.manager = manager or ProcessManager()
        self._definition = ToolDefinition(
            id="process.exec",
            version=1,
            kind=ToolKind.PROCESS,
            description="Run an allowlisted executable with an argv vector (no shell)",
            risk_class=RiskClass.R1,
            network_access=False,
            timeout_seconds=30,
            output_limit_bytes=65536,
            parameters={
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            capabilities=("process",),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        argv = [str(argument) for argument in params["argv"]]
        result = asyncio.run(
            self.manager.run(argv, timeout_seconds=float(self._definition.timeout_seconds))
        )
        stderr = result.stderr
        if result.timed_out:
            stderr += "\n[process timed out]"
        if result.truncated:
            stderr += "\n[output truncated]"
        return ToolOutput(
            stdout=result.stdout,
            stderr=stderr,
            exit_code=result.exit_code if result.exit_code is not None else -1,
            timed_out=result.timed_out,
        )


class HttpFetchTool:
    def __init__(
        self,
        broker_factory: Callable[[ScopeContract], HttpEgressBroker] = HttpEgressBroker,
    ) -> None:
        self._broker_factory = broker_factory
        self._definition = ToolDefinition(
            id="http.fetch",
            version=1,
            kind=ToolKind.HTTP,
            description="Fetch an in-scope URL over HTTP(S) with bounded output",
            risk_class=RiskClass.R2,
            network_access=True,
            timeout_seconds=15,
            output_limit_bytes=65536,
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                    "method": {"type": "string", "enum": ["GET", "HEAD"], "default": "GET"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            capabilities=("http",),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        broker = self._broker_factory(scope)
        response = broker.fetch(
            str(params["url"]),
            method=str(params.get("method", "GET")),
            timeout=float(self._definition.timeout_seconds),
            max_bytes=self._definition.output_limit_bytes,
        )
        stderr = f"HTTP {response.status} {response.reason} {response.url}".strip()
        if response.redirects:
            stderr += f" (redirects: {' -> '.join(response.redirects)})"
        if response.truncated:
            stderr += " [truncated]"
        return ToolOutput(
            stdout=response.body.decode("utf-8", errors="replace"),
            stderr=stderr,
            exit_code=0,
        )


class SandboxExecTool:
    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        if runner is None:
            from samurai_ai.services.vault import load_master_key

            runner = DockerSandboxRunner(
                ProcessManager(allowed_executables=frozenset({"docker"})),
                sandbox_allowlist_path(),
                lambda: load_master_key(vault_key_path()),
            )
        self.runner = runner
        self._definition = ToolDefinition(
            id="sandbox.exec",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Run a profile-allowlisted executable in a signed, "
            "networkless Docker sandbox",
            risk_class=RiskClass.R2,
            network_access=False,
            timeout_seconds=90,
            output_limit_bytes=65536,
            parameters={
                "type": "object",
                "properties": {
                    "profile": {"type": "string", "minLength": 1},
                    "executable": {"type": "string", "minLength": 1},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["profile", "executable"],
                "additionalProperties": False,
            },
            capabilities=("sandbox",),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def preflight(self, params: dict[str, Any]) -> None:
        profile = get_profile(str(params["profile"]))
        executable = str(params["executable"])
        args = [str(argument) for argument in params.get("args", [])]
        self.runner.build_argv(profile, "preflight", executable, args)
        asyncio.run(self.runner.preflight(profile))

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        profile = get_profile(str(params["profile"]))
        executable = str(params["executable"])
        args = [str(argument) for argument in params.get("args", [])]
        run_id = str(params.get("_run_id") or uuid4().hex[:12])
        result = self.runner.run(profile, run_id, executable, args)
        stderr = result.stderr
        if result.timed_out:
            stderr += "\n[sandbox timed out]"
        if result.truncated:
            stderr += "\n[output truncated]"
        return ToolOutput(
            stdout=result.stdout,
            stderr=stderr,
            exit_code=result.exit_code if result.exit_code is not None else -1,
            timed_out=result.timed_out,
        )


def default_registry() -> ToolRegistry:
    return ToolRegistry(
        adapters=(LocalEchoTool(), ProcessExecTool(), HttpFetchTool(), SandboxExecTool())
    )


__all__ = [
    "HttpFetchTool",
    "LocalEchoTool",
    "ProcessExecTool",
    "SandboxExecTool",
    "ToolAdapter",
    "ToolOutput",
    "ToolRegistry",
    "default_registry",
]
