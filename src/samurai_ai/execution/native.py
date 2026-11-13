"""Native execution: the tool on this machine, through argv and nothing else.

The executable and the argv frame come from the catalogue, so the model still
only supplies values. Governance is untouched: the service still checks scope,
asks for the approval and stores the artifacts — this class adds a transport, not
a bypass. Output goes to the run artifacts in full while the model receives a
summary, and a missing binary is its own status instead of a generic failure.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from samurai_ai.config import config_directory
from samurai_ai.domain.execution import (
    ToolDefinition,
    ToolExecutionError,
    ToolValidationError,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.tool_manifest import ToolManifest
from samurai_ai.execution.manifests import PARSERS
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.registry import ToolOutput

#: not the whole environment: a tool sees these and nothing else by default
ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "USER", "SHELL")
_PLACEHOLDER_OPEN = "{"


class NativeToolError(ToolExecutionError):
    pass


class MissingBinaryError(NativeToolError):
    """Detected before spawning: the caller can offer a provision."""

    def __init__(self, tool_id: str, executable: str) -> None:
        self.tool_id = tool_id
        self.executable = executable
        super().__init__(
            f"{tool_id}: {executable!r} is not installed on this machine "
            "(native path unavailable)"
        )


def render_native_argv(
    recipe: ToolManifest, params: dict[str, Any]
) -> list[str]:
    """argv from the recipe; booleans become flags, arrays join with the
    parameter's `separator` (one token) unless `argv_split` asks for one token
    per item — nmap takes its targets as separate words, not a comma list."""
    argv: list[str] = []
    for token in recipe.argv:
        name = token[1:-1] if token.startswith(_PLACEHOLDER_OPEN) and token.endswith("}") else ""
        if name == "__path" and params.get("__path"):
            # project-local: the installed script path is the first argument
            argv.append(str(params["__path"]))
            continue
        if name and name in params:
            value = params[name]
            if isinstance(value, bool):
                # a boolean placeholder stands for a whole flag token: «{service}»
                if value:
                    argv.append(str(value))
                continue
            if isinstance(value, (list, tuple)):
                spec = recipe.params.get(name)
                if spec is not None and spec.argv_split:
                    argv.extend(str(item) for item in value)
                else:
                    separator = spec.separator if spec is not None else ","
                    argv.append(separator.join(str(item) for item in value))
                continue
            argv.append(str(value))
            continue
        argv.append(token)
    return argv


def _env_allowlist() -> dict[str, str]:
    import os

    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    # a tool writing a config where it likes still lands inside the run's tmp
    return env


class NativeTool:
    """A catalogue recipe bound to its typed adapter's definition."""

    #: tells the service this adapter builds its own argv (no sandbox runner)
    is_native = True

    def __init__(
        self,
        recipe: ToolManifest,
        definition: ToolDefinition,
        runner: ProcessManager | None = None,
        *,
        executable_path: str | None = None,
        script_dir: Path | None = None,
    ) -> None:
        self.recipe = recipe
        self._definition = definition
        self._runner = runner
        self._path = executable_path
        self._script_dir = script_dir

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def script(self) -> Path | None:
        """Where a project-local install puts the tool (None for other recipes)."""
        if not self.recipe.execution.project_local:
            return None
        directory = self._script_dir or (config_directory() / "tools" / "bin")
        return directory / f"{self.recipe.id.replace('.', '-')}.py"

    @property
    def path(self) -> str:
        if self._path is None:
            self._path = shutil.which(self.recipe.executable) or ""
        return self._path

    @property
    def timeout_seconds(self) -> int:
        return self.recipe.limits.timeout_seconds

    def resolved_available(self) -> bool:
        if self.recipe.execution.project_local:
            script = self.script
            return bool(self.path and script is not None and script.exists())
        return bool(self.path)

    def _runner_manager(self) -> ProcessManager:
        if self._runner is None:
            allowed = {self.recipe.executable}
            self._runner = ProcessManager(allowed_executables=frozenset(allowed))
        return self._runner

    def _with_script(self, params: dict[str, Any]) -> dict[str, Any]:
        script = self.script
        if script is None:
            return params
        if not script.exists():
            raise MissingBinaryError(self.recipe.id, script.name)
        return {**params, "__path": str(script)}

    def preflight(self, params: dict[str, Any]) -> None:
        """Everything that can fail is checked before the approval is asked."""
        render_native_argv(self.recipe, self._with_script(params))
        if not self.path:
            raise MissingBinaryError(self.recipe.id, self.recipe.executable)

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        executable = self.path or self.recipe.executable
        if not self.path:
            raise MissingBinaryError(self.recipe.id, self.recipe.executable)
        argv = [executable, *render_native_argv(self.recipe, self._with_script(params))]
        if any("\x00" in argument for argument in argv):
            raise ToolValidationError(f"{self.recipe.id}: argv cannot contain NUL")
        result = asyncio.run(
            self._runner_manager().run(
                argv,
                timeout_seconds=float(self.recipe.limits.timeout_seconds),
                env=_env_allowlist(),
                inherit_env=False,
            )
        )
        stderr = result.stderr
        if result.timed_out:
            stderr += "\n[native run timed out]"
        if result.stalled:
            stderr += "\n[native run stalled]"
        if result.truncated:
            stderr += "\n[output truncated]"
        if result.exit_code == 127:
            stderr += f"\n[missing binary: {self.recipe.executable}]"
        lines = PARSERS[self.recipe.parser](result.stdout, self.recipe.summary_limit)
        stdout = ""
        if lines:
            stdout = "# summary\n" + "\n".join(lines) + "\n\n# raw\n"
        stdout += result.stdout
        return ToolOutput(
            stdout=stdout,
            stderr=stderr,
            exit_code=result.exit_code if result.exit_code is not None else -1,
            timed_out=result.timed_out,
        )


__all__ = [
    "ENV_ALLOWLIST",
    "MissingBinaryError",
    "NativeTool",
    "NativeToolError",
    "render_native_argv",
]
