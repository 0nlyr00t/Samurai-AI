"""Versioned, fail-closed tool registry with the built-in Stage 2a adapters.

Tools are static, code-defined adapters — there is no API to register
arbitrary command strings or dynamic callables at runtime.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from samurai_ai.config import sandbox_allowlist_path, vault_key_path
from samurai_ai.domain.execution import (
    NetworkAccess,
    ToolDefinition,
    ToolExecutionError,
    ToolKind,
    ToolNotFoundError,
    ToolScopeError,
    ToolValidationError,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.permission import command_window
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.http import HttpEgressBroker
from samurai_ai.execution.process import DEFAULT_STALL_SECONDS, ProcessManager
from samurai_ai.execution.relay import normalize_relayed
from samurai_ai.execution.sandbox import DockerSandboxRunner, get_profile


@dataclass(frozen=True)
class ToolOutput:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    secrets: tuple[str, ...] = ()
    egress_events: tuple[dict[str, Any], ...] = ()
    #: request/response pairs the service records as evidence (research reads)
    traffic: tuple[dict[str, Any], ...] = ()


class ToolAdapter(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput: ...


class DynamicToolResolver(Protocol):
    def resolve(self, tool_id: str) -> ToolAdapter | None: ...

    def definitions(self) -> list[ToolDefinition]: ...


class ToolRegistry:
    def __init__(
        self,
        adapters: Iterable[ToolAdapter] = (),
        dynamic_resolver: DynamicToolResolver | None = None,
    ) -> None:
        self._adapters: dict[tuple[str, int], ToolAdapter] = {}
        #: native variants by tool id: same definition, a run on this machine
        self._native: dict[str, ToolAdapter] = {}
        self._dynamic_resolver = dynamic_resolver
        for adapter in adapters:
            self.register(adapter)

    def register_native(self, adapter: ToolAdapter, *, standalone: bool = False) -> None:
        """A native path for a registered tool, or the only path for a new one."""
        tool_id = adapter.definition.id
        known = any(registered == tool_id for registered, _ in self._adapters)
        if not known:
            if not standalone:
                raise ToolValidationError(
                    f"native adapter {tool_id} has no registered definition to stand for"
                )
            self.register(adapter)
        self._native[tool_id] = adapter

    def native_for(self, tool_id: str) -> ToolAdapter | None:
        """The native variant, when this machine can actually run it."""
        adapter = self._native.get(tool_id)
        if adapter is None:
            return None
        available = getattr(adapter, "resolved_available", None)
        if callable(available) and not available():
            return None
        return adapter

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
        if tool_id.startswith("mcp."):
            if version is not None and version != 1:
                raise ToolNotFoundError(f"tool not found: {tool_id} v{version}")
            if self._dynamic_resolver is not None:
                adapter = self._dynamic_resolver.resolve(tool_id)
                if adapter is not None:
                    return adapter
            raise ToolNotFoundError(f"tool not found: {tool_id}")
        versions = sorted(v for (registered_id, v) in self._adapters if registered_id == tool_id)
        if not versions:
            raise ToolNotFoundError(f"tool not found: {tool_id}")
        resolved = version if version is not None else versions[-1]
        if version is None:
            native = self.native_for(tool_id)
            if native is not None:
                return native
        adapter = self._adapters.get((tool_id, resolved))
        if adapter is None:
            raise ToolNotFoundError(f"tool not found: {tool_id} v{resolved}")
        return adapter

    def definitions(self) -> list[ToolDefinition]:
        # native variants share the definition of their container sibling
        static = [
            adapter.definition
            for _, adapter in sorted(self._adapters.items(), key=lambda item: item[0])
        ]
        if self._dynamic_resolver is not None:
            return static + self._dynamic_resolver.definitions()
        return static

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
        if definition.kind == ToolKind.HTTP and definition.network is not NetworkAccess.DIRECT:
            raise ToolValidationError(
                f"http tool {definition.id} must declare network=direct"
            )
        if definition.kind == ToolKind.MCP and definition.network is NetworkAccess.PROXIED:
            raise ToolValidationError(
                f"mcp tool {definition.id} reaches the network directly or not at all"
            )
        if definition.kind in (ToolKind.LOCAL, ToolKind.PROCESS) and (
            definition.network is not NetworkAccess.NONE
        ):
            raise ToolValidationError(
                f"{definition.kind.value} tool {definition.id} cannot declare network access"
            )
        if definition.read_only and definition.risk_class in (RiskClass.R3, RiskClass.R4):
            raise ToolValidationError(
                f"tool {definition.id} is {definition.risk_class.value} and cannot be read-only"
            )
        if definition.risk_class is RiskClass.R0 and not definition.read_only:
            raise ToolValidationError(
                f"r0 tool {definition.id} must be declared read-only"
            )
        if (
            definition.kind == ToolKind.SANDBOX
            and definition.network is NetworkAccess.DIRECT
        ):
            # direct-class profiles run on bridge networking: nothing enforces
            # egress but the adapter's own scope check.
            pass


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
        if "maxItems" in spec and len(value) > int(spec["maxItems"]):
            raise ToolValidationError(f"{label} allows at most {spec['maxItems']} items")
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
            read_only=True,
            network=NetworkAccess.NONE,
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
            description=(
                "Run one allowlisted executable with an argv vector, no shell"
                f" (allowlist: {', '.join(sorted(self.manager.allowed_executables))})"
            ),
            risk_class=RiskClass.R1,
            read_only=True,
            network=NetworkAccess.NONE,
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


class ShellExecTool:
    """Run an arbitrary command line through the host shell.

    Unlike ProcessExecTool (allowlisted argv, no shell), this tool exists for
    the interactive agent loop where the model asks to run real commands.
    It is risk class R3: every call requires an operator approval (or yolo
    mode) and is recorded with redacted, bounded output artifacts.
    """

    #: the operation's relay prefixes every command on the host side too
    relay_supported = True

    def __init__(self, manager: ProcessManager | None = None) -> None:
        self.manager = manager or ProcessManager(
            allowed_executables=frozenset({"bash", "sh"})
        )
        self.relay = ""
        self._definition = ToolDefinition(
            id="shell.exec",
            version=1,
            kind=ToolKind.PROCESS,
            description=(
                "Run a shell command on the operator host as bash -lc; use this when a "
                "pipeline, redirection or substitution is needed"
            ),
            risk_class=RiskClass.R3,
            network=NetworkAccess.NONE,
            timeout_seconds=120,
            output_limit_bytes=65536,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8192,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            capabilities=("shell",),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        command = str(params["command"])
        if self.relay:
            command = normalize_relayed(self.relay, command)
            # a relayed command's silence is the far side's pace, not a stall:
            # the stall watchdog used to kill slow VPN runs and feed the breaker
            self.manager.stall_seconds = 0.0
        else:
            self.manager.stall_seconds = DEFAULT_STALL_SECONDS
        shell = "bash" if shutil.which("bash") else "sh"
        result = asyncio.run(
            self.manager.run(
                [shell, "-lc", command],
                # a live sweep gets minutes, a banner grab seconds: one flat 120 s
                # window killed every `nmap -p-` while stalling every `curl`
                timeout_seconds=command_window(command, relayed=bool(self.relay)),
            )
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
    #: the tool can be pushed behind a relay prefix when the host has no route
    relay_supported = True

    def __init__(
        self,
        broker_factory: Callable[[ScopeContract], HttpEgressBroker] = HttpEgressBroker,
        manager: ProcessManager | None = None,
    ) -> None:
        self._broker_factory = broker_factory
        self._manager = manager or ProcessManager(allowed_executables=frozenset({"bash"}))
        self.relay = ""
        self._definition = ToolDefinition(
            id="http.fetch",
            version=1,
            kind=ToolKind.HTTP,
            description="Fetch an in-scope URL over HTTP(S) with bounded output",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.DIRECT,
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
        if self.relay:
            return self._invoke_relayed(params, scope)
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


    def _invoke_relayed(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        """The host cannot connect: let curl do it on the far side of the relay."""
        from samurai_ai.execution.relay import relayed_fetch

        url = str(params["url"])
        decision = scope.evaluate(url)
        if not decision.allowed:
            raise ToolScopeError(f"{url} is out of scope: {decision.detail}")
        response = relayed_fetch(
            self.relay,
            url,
            method=str(params.get("method", "GET")),
            timeout=float(self._definition.timeout_seconds),
            max_bytes=self._definition.output_limit_bytes,
            manager=self._manager,
        )
        if response.status == 0:
            raise ToolExecutionError(f"relayed fetch of {url} returned no status line")
        return ToolOutput(
            stdout=response.body.decode("utf-8", errors="replace"),
            stderr=f"HTTP {response.status} {url} (relayed)",
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
        self._notice: str | None = None
        self._definition = ToolDefinition(
            id="sandbox.exec",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Run a profile-allowlisted executable in a signed, "
            "sandbox in a signed allowlist profile (network only via the scoped proxy)",
            risk_class=RiskClass.R2,
            network=NetworkAccess.PROXIED,
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
        self.runner.validate_invocation(profile, executable, args)
        self._notice = asyncio.run(self.runner.preflight(profile))

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        profile = get_profile(str(params["profile"]))
        executable = str(params["executable"])
        args = [str(argument) for argument in params.get("args", [])]
        run_id = str(params.get("_run_id") or uuid4().hex[:12])
        outcome = self.runner.run(profile, run_id, executable, args, scope)
        result = outcome.result
        stderr = result.stderr
        if self._notice is not None:
            stderr = f"[allowlist] {self._notice}\n{stderr}"
        if result.timed_out:
            stderr += "\n[sandbox timed out]"
        if result.truncated:
            stderr += "\n[output truncated]"
        if outcome.egress_events:
            allowed = sum(
                1 for event in outcome.egress_events if event["verdict"] == "allowed"
            )
            stderr += (
                f"\n[egress: {len(outcome.egress_events)} proxied request(s), "
                f"{allowed} allowed]"
            )
        return ToolOutput(
            stdout=result.stdout,
            stderr=stderr,
            exit_code=result.exit_code if result.exit_code is not None else -1,
            timed_out=result.timed_out,
            secrets=(outcome.proxy_token,) if outcome.proxy_token else (),
            egress_events=outcome.egress_events,
        )


def _manifest_tools() -> tuple[Any, ...]:
    """Tools installed as manifests; code adapters win on an id collision."""
    from samurai_ai.config import tools_directory
    from samurai_ai.execution.manifests import ManifestTool
    from samurai_ai.services.manifests import ManifestStore

    return tuple(ManifestTool(manifest) for manifest in ManifestStore(tools_directory()).load_all())


def default_registry(dynamic_resolver: DynamicToolResolver | None = None) -> ToolRegistry:
    from samurai_ai.execution.exploits import (
        ExploitAnalyzeTool,
        ExploitBuildTool,
        ExploitRunTool,
    )
    from samurai_ai.execution.remote import (
        AdbTool,
        RdpCheckTool,
        SmbListTool,
        SshExecTool,
    )
    from samurai_ai.execution.security import (
        DnsxResolveTool,
        FfufFuzzTool,
        HttpxProbeTool,
        NmapScanTool,
        NucleiScanTool,
        ResearchReadTool,
        ResearchSearchTool,
        SubfinderPassiveTool,
    )
    from samurai_ai.execution.virtualization import VmControlTool, VmDiskTool

    registry = ToolRegistry(
        adapters=(
            LocalEchoTool(),
            ProcessExecTool(),
            ShellExecTool(),
            HttpFetchTool(),
            SandboxExecTool(),
            HttpxProbeTool(),
            NucleiScanTool(),
            SubfinderPassiveTool(),
            DnsxResolveTool(),
            NmapScanTool(),
            FfufFuzzTool(),
            ResearchSearchTool(),
            ResearchReadTool(),
            SshExecTool(),
            SmbListTool(),
            RdpCheckTool(),
            AdbTool(),
            VmControlTool(),
            VmDiskTool(),
            ExploitAnalyzeTool(),
            PatchDiffTool(),
            ExploitBuildTool(),
            ExploitRunTool(),
            *_manifest_tools(),
        ),
        dynamic_resolver=dynamic_resolver,
    )
    for adapter, standalone in _native_tools(registry):
        registry.register_native(adapter, standalone=standalone)
    return registry


def _native_tools(registry: ToolRegistry) -> tuple[Any, ...]:
    """Native variants from the reviewed catalogue, bound to their own definition.

    A recipe only becomes usable when the machine has the binary and the adapter
    it describes is registered, so the parameter schema stays the adapter's.
    """
    from samurai_ai.execution.native import NativeTool
    from samurai_ai.services.catalog import RecipeCatalog, recipes_directory

    tools: list[Any] = []
    for tool_id, recipe in RecipeCatalog(recipes_directory()).load_all().items():
        if not (recipe.execution.native or recipe.execution.project_local):
            continue
        if not recipe.execution.native_preferred:
            continue
        standalone = recipe.execution.project_local
        try:
            definition = registry.get(tool_id).definition
        except ToolNotFoundError:
            # a tool with no typed adapter yet is native-only: its own definition
            definition = recipe.definition()
            standalone = True
        tools.append((NativeTool(recipe, definition), standalone))
    return tuple(tools)


__all__ = [
    "DynamicToolResolver",
    "HttpFetchTool",
    "LocalEchoTool",
    "ProcessExecTool",
    "ShellExecTool",
    "SandboxExecTool",
    "ToolAdapter",
    "ToolOutput",
    "ToolRegistry",
    "default_registry",
]


class PatchDiffTool:
    """Read a unified diff (plus an optional advisory) into a 1-day hypothesis."""

    def __init__(self) -> None:
        self._definition = ToolDefinition(
            id="exploit.patch-diff",
            version=1,
            kind=ToolKind.LOCAL,
            description=(
                "Analyze a patch diff and an advisory into a vulnerability "
                "hypothesis: changed files, touched functions, added guards."
            ),
            risk_class=RiskClass.R1,
            read_only=True,
            network=NetworkAccess.NONE,
            timeout_seconds=30,
            output_limit_bytes=16384,
            parameters={
                "type": "object",
                "properties": {
                    "diff": {"type": "string", "minLength": 1, "maxLength": 200000},
                    "advisory": {"type": "string", "maxLength": 20000},
                },
                "required": ["diff"],
                "additionalProperties": False,
            },
            capabilities=("exploit", "analysis"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        from samurai_ai.analysis.patchdiff import analyze_patch, report

        diff_text = str(params["diff"])
        advisory = str(params.get("advisory", ""))
        analysis = analyze_patch(diff_text, advisory)
        return ToolOutput(stdout=report(analysis), stderr="", exit_code=0)
