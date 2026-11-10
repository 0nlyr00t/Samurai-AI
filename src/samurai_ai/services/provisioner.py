"""The provisioner: plan, ask, install through argv, verify, refresh.

Only a `tool_id` comes in. Everything else — package, manager, argv, privilege —
is read from the reviewed recipe, so no model text ever becomes an install
command. The plan is built and shown before anything changes; the approval is its
own approval, separate from tool runs, and YOLO does not cover it.

Refusals are explicit and terminal: offline, missing privilege, unsupported
platform and a failed healthcheck each stop with a reason and a manual command.
A partially installed package is never `ready`, and a retry after `failed` needs
a new decision.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from samurai_ai.domain.environment import EnvironmentDetector, EnvironmentProfile, OsFamily
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.provision import (
    ProvisionError,
    ProvisionLockedError,
    ProvisionPlan,
    ProvisionRefusedError,
    ProvisionRun,
    ProvisionState,
    ProvisionStatus,
)
from samurai_ai.domain.tool_manifest import PackageSpec, ToolManifest
from samurai_ai.execution.process import (
    CircuitBreakerOpenError,
    ProcessManager,
    ProcessResult,
)
from samurai_ai.services.catalog import RecipeCatalog
from samurai_ai.services.fabric import ProvisionReason, ToolResolver, platform_key

#: package-manager binaries the provisioner may spawn, and nothing else
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: log levels a tool prefixes its own output with
_NOISE_PREFIXES = ("[ERR", "[WRN", "[WAR", "[DBG")
_VERSIONISH = re.compile(r"version|\bv\d|\d+\.\d+", re.I)

_SHELL_SAFE = frozenset({"apt-get", "brew", "python3", "sudo", "cp", "mkdir"})
_INSTALL_TIMEOUT = 600.0
_HEALTHCHECK_TIMEOUT = 30.0


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def version_line(text: str) -> str:
    """The line that reports a version, not the tool's warning noise.

    Tools prefix output with their own levels (`[ERR]`, `[INF]`); taking the
    first line recorded a colour-escaped error as the version.
    """
    lines = [line.strip() for line in strip_ansi(text).splitlines() if line.strip()]
    for line in lines:
        if line.startswith(_NOISE_PREFIXES):
            continue
        if _VERSIONISH.search(line):
            return line[:200]
    for line in lines:
        if not line.startswith(_NOISE_PREFIXES):
            return line[:200]
    return lines[0][:200] if lines else ""


class Provisioner:
    def __init__(
        self,
        *,
        operations: Any,
        kernel: Any,
        catalog: RecipeCatalog | None = None,
        manager: ProcessManager | None = None,
        tools_dir: Path | None = None,
        templates: Path | None = None,
        runner: Callable[[list[str], float], ProcessResult] | None = None,
        state_file: Path | None = None,
        detector: EnvironmentDetector | None = None,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.catalog = catalog or RecipeCatalog()
        self.manager = manager or ProcessManager(allowed_executables=_SHELL_SAFE)
        self.tools_dir = tools_dir
        self.templates = templates
        self._runner = runner
        #: the detector is shared so a provision can refresh what it changed
        self.detector = detector or EnvironmentDetector()
        self.states: dict[str, ProvisionState] = {}
        #: registries whose native paths this provisioner may invalidate
        self._registries: list[Any] = []
        default_state = (
            self.tools_dir.parent / "provision-state.json" if self.tools_dir else None
        )
        self.state_file = state_file or default_state
        self._load_state()

    # --- planning ---------------------------------------------------------

    def plan(self, tool_id: str, profile: EnvironmentProfile) -> ProvisionPlan:
        """Build the plan; touches nothing but memory."""
        recipe = self._recipe(tool_id)
        resolver = ToolResolver(profile, project_local_dir=self._scripts_dir())
        provisionable, reason, manual = resolver.can_provision(recipe)
        spec = recipe.package_for(platform_key(profile))
        if spec is None or not provisionable:
            # the resolver's text carries the manual command or the why; keep both
            detail = manual or reason.value
            command = manual if manual.startswith(("sudo", "brew")) else ""
            raise ProvisionRefusedError(reason.value, detail, command)
        commands = self._commands(recipe, spec, profile)
        healthcheck = self._healthcheck_command(recipe)
        return ProvisionPlan(
            tool_id=tool_id,
            recipe_version=recipe.version,
            platform=platform_key(profile),
            arch=profile.arch,
            package=spec.package,
            manager=spec.manager,
            commands=tuple(tuple(argv) for argv in commands),
            healthcheck=healthcheck,
            privileged=spec.sudo,
            requires_docker=recipe.provision.requires_docker,
            requires_gpu=recipe.provision.requires_gpu,
            limitations=recipe.provision.limitations,
            reproducible=spec.reproducible,
            manual_command=manual,
        )

    def _commands(
        self, recipe: ToolManifest, spec: PackageSpec, profile: EnvironmentProfile
    ) -> list[list[str]]:
        if spec.manager == "project-local":
            script = self._template(recipe, spec)
            target = self._script_path(recipe)
            return [["mkdir", "-p", str(target.parent)], ["cp", str(script), str(target)]]
        manager_bin = "brew" if spec.manager == "brew" else "apt-get"
        argv: list[str] = [manager_bin]
        if spec.manager == "apt":
            argv += ["install", "-y", "--no-install-recommends"]
        else:
            argv += ["install"]
        argv += [spec.package, *spec.extra]
        if spec.sudo:
            argv = ["sudo", "-n", *argv]
        return [argv]

    def _healthcheck_command(self, recipe: ToolManifest) -> tuple[str, ...]:
        flags = recipe.provision.healthcheck
        if not flags:
            return ()
        return (recipe.executable, *flags)

    def _recipe(self, tool_id: str) -> ToolManifest:
        recipe = self.catalog.get(tool_id)
        if recipe is None:
            raise ProvisionRefusedError(
                "manual-only", f"{tool_id} is not in the reviewed catalogue"
            )
        return recipe

    def _template(self, recipe: ToolManifest, spec: PackageSpec) -> Path:
        if self.templates is None:
            raise ProvisionRefusedError(
                "manual-only", f"{recipe.id}: no template directory configured"
            )
        source = self.templates / spec.package
        if not source.exists():
            raise ProvisionRefusedError("manual-only", f"{recipe.id}: template {source} is missing")
        return source

    def _scripts_dir(self) -> Path | None:
        return None if self.tools_dir is None else self.tools_dir / "bin"

    def _script_path(self, recipe: ToolManifest) -> Path:
        directory = self._scripts_dir()
        if directory is None:
            raise ProvisionRefusedError("manual-only", f"{recipe.id}: no tools directory")
        return directory / f"{recipe.id.replace('.', '-')}.py"

    # --- state and lock ---------------------------------------------------

    def _load_state(self) -> None:
        """Persisted per-tool state: the version that landed, not a promise."""
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for entry in payload.get("tools", []):
            try:
                state = ProvisionState.model_validate(entry)
            except Exception:  # noqa: BLE001 - a stale entry is not fatal
                continue
            # an interrupted install must not look locked forever
            if state.locked:
                state.status = ProvisionStatus.FAILED
                state.error = state.error or "interrupted"
            self.states[state.tool_id] = state

    def _save_state(self) -> None:
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "tools": [
                json.loads(state.model_dump_json())
                for state in self.states.values()
                if state.status is not ProvisionStatus.PLANNED
            ],
        }
        self.state_file.write_text(json.dumps(payload, indent=2))

    def needs_decision(self, tool_id: str) -> bool:
        """A failed attempt is not retried on its own: it needs a new decision."""
        state = self.states.get(tool_id)
        return state is not None and state.status is ProvisionStatus.FAILED


    def state(self, tool_id: str) -> ProvisionState:
        return self.states.setdefault(tool_id, ProvisionState(tool_id=tool_id))

    def _acquire(self, tool_id: str) -> ProvisionState:
        state = self.state(tool_id)
        if state.locked:
            raise ProvisionLockedError(f"{tool_id} is already {state.status.value}")
        state.status = ProvisionStatus.AWAITING_APPROVAL
        state.updated_at = datetime.now(UTC)
        return state

    # --- running ----------------------------------------------------------

    def run(
        self,
        operation_id: str,
        tool_id: str,
        profile: EnvironmentProfile,
        *,
        approved: bool = True,
        dry_run: bool = False,
    ) -> ProvisionRun:
        """Plan and (unless dry-run) execute. A dry run changes nothing at all."""
        plan = self.plan(tool_id, profile)
        run = ProvisionRun(run_id=f"prov_{uuid4().hex[:12]}", tool_id=tool_id, plan=plan)
        state = self.state(tool_id)
        state.plan = plan
        state.run_id = run.run_id
        state.recipe_version = plan.recipe_version
        if dry_run:
            return run
        # an install in flight owns the tool: its result is not known yet
        already = None if state.locked else self._already_installed(tool_id, profile)
        if already is not None:
            # a second provision of a working tool is a no-op, not a reinstall
            state.status = ProvisionStatus.READY
            state.installed_version = already
            state.updated_at = datetime.now(UTC)
            run.status = ProvisionStatus.READY
            run.finished_at = datetime.now(UTC)
            self._event(operation_id, run, "provision.skipped", {"reason": "already installed"})
            return run
        if not approved:
            state.status = ProvisionStatus.AWAITING_APPROVAL
            raise ProvisionRefusedError("awaiting-approval", "the plan needs an approval")
        self._execute(operation_id, run, state)
        return run

    def _already_installed(self, tool_id: str, profile: EnvironmentProfile) -> str | None:
        """The version string when the tool already answers its healthcheck."""
        recipe = self.catalog.get(tool_id)
        if recipe is None:
            return None
        resolver = ToolResolver(profile, project_local_dir=self._scripts_dir())
        resolution = resolver.resolve(tool_id, recipe)
        if resolution.reason is not ProvisionReason.INSTALLED:
            return None
        if not resolution.path:
            return None
        output, healthy = self._healthcheck(
            ProvisionPlan(
                tool_id=tool_id,
                recipe_version=recipe.version,
                platform=platform_key(profile),
                arch=profile.arch,
                package="",
                manager="",
                healthcheck=self._healthcheck_command(recipe),
            )
        )
        return version_line(output) if healthy and output.strip() else None

    def _execute(
        self, operation_id: str, run: ProvisionRun, state: ProvisionState
    ) -> None:
        state = self._acquire(run.tool_id)
        state.attempts += 1
        run.status = ProvisionStatus.INSTALLING
        self._event(operation_id, run, "provision.started", {"plan": self._plan_dict(run)})
        for argv in run.plan.commands:
            result = self._run_command(argv, _INSTALL_TIMEOUT)
            run.command_results.append(
                {
                    "argv": list(argv),
                    "exit_code": result.exit_code,
                    "stdout": result.stdout[-2000:],
                    "stderr": result.stderr[-2000:],
                }
            )
            if result.exit_code != 0:
                self._fail(operation_id, run, state, self._why(argv, result))
                return
        run.status = ProvisionStatus.VERIFYING
        state.status = ProvisionStatus.VERIFYING
        output, healthy = self._healthcheck(run.plan)
        run.healthcheck_output = output
        if not healthy:
            self._fail(
                operation_id,
                run,
                state,
                f"healthcheck failed: {' '.join(run.plan.healthcheck)}",
                status=ProvisionStatus.FAILED,
            )
            return
        state.status = ProvisionStatus.READY
        state.error = ""
        state.installed_version = version_line(output)
        state.resolved_path = (
            shutil.which(run.plan.healthcheck[0]) if run.plan.healthcheck else ""
        ) or ""
        state.updated_at = datetime.now(UTC)
        run.status = ProvisionStatus.READY
        run.finished_at = datetime.now(UTC)
        self._save_state()
        # the machine changed: the cached profile and any memoized binary path
        # are stale, so the tool is usable without restarting the app
        self.detector.detect(refresh=True)
        self.refresh_registry()
        self._event(
            operation_id,
            run,
            "provision.ready",
            {"version": state.installed_version, "path": state.resolved_path},
        )

    def _healthcheck(self, plan: ProvisionPlan) -> tuple[str, bool]:
        """Read-only probe: a response proves the install, not the exit alone."""
        if not plan.healthcheck:
            return "", True
        argv = list(plan.healthcheck)
        path = shutil.which(argv[0])
        if path:
            argv[0] = path
        result = self._run_command(argv, _HEALTHCHECK_TIMEOUT)
        return (result.stdout + result.stderr).strip(), result.exit_code == 0

    def _run_command(
        self, argv: list[str] | tuple[str, ...], timeout: float
    ) -> ProcessResult:
        if self._runner is not None:
            return self._runner(list(argv), timeout)
        # the healthcheck binary is whatever the recipe names: allow it, and only
        # it, on top of the package managers, so argv stays fail-closed
        name = Path(argv[0]).name
        if name not in self.manager.allowed_executables:
            self.manager.allowed_executables = frozenset(
                {*self.manager.allowed_executables, name}
            )
        try:
            return asyncio.run(
                self.manager.run(list(argv), timeout_seconds=timeout, inherit_env=True)
            )
        except CircuitBreakerOpenError as exc:
            return ProcessResult(exit_code=127, stdout="", stderr=str(exc))

    @staticmethod
    def _why(argv: list[str] | tuple[str, ...], result: ProcessResult) -> str:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1][:200] if detail else "no output"
        return f"{' '.join(argv[:3])} exited {result.exit_code}: {tail}"

    def _fail(
        self,
        operation_id: str,
        run: ProvisionRun,
        state: ProvisionState,
        error: str,
        *,
        status: ProvisionStatus = ProvisionStatus.FAILED,
    ) -> None:
        state.status = status
        state.error = error
        state.updated_at = datetime.now(UTC)
        run.status = status
        run.error = error
        run.finished_at = datetime.now(UTC)
        self._save_state()
        self._event(operation_id, run, "provision.failed", {"error": error})

    def watch(self, registry: Any) -> None:
        """Let a provision invalidate this registry's memoized native paths."""
        if registry not in self._registries:
            self._registries.append(registry)

    def refresh_registry(self) -> None:
        """Drop memoized executable paths so a new binary is seen at once."""
        for registry in self._registries:
            for tool_id in list(getattr(registry, "_native", {})):
                adapter = registry._native[tool_id]
                if hasattr(adapter, "_path"):
                    adapter._path = None

    def _plan_dict(self, run: ProvisionRun) -> dict[str, object]:
        plan = run.plan
        return {
            "run_id": run.run_id,
            "tool_id": plan.tool_id,
            "recipe_version": plan.recipe_version,
            "platform": plan.platform,
            "package": plan.package,
            "manager": plan.manager,
            "commands": [list(argv) for argv in plan.commands],
            "privileged": plan.privileged,
            "healthcheck": list(plan.healthcheck),
            "reproducible": plan.reproducible,
        }

    def _event(
        self,
        operation_id: str,
        run: ProvisionRun,
        event_type: str,
        extra: dict[str, object],
    ) -> None:
        payload = {"tool_id": run.tool_id, **extra}
        try:
            self.operations.repository.append_event(
                OperationEvent(
                    operation_id=operation_id,
                    event_type=event_type,
                    payload={key: value for key, value in payload.items() if value != ""},
                )
            )
        except Exception:  # noqa: BLE001 - the journal must not fail a provision
            pass

    # --- description ------------------------------------------------------

    @staticmethod
    def describe(plan: ProvisionPlan) -> list[str]:
        """The dry-run text: platform, package, commands, privilege, check."""
        lines = [
            f"tool      {plan.tool_id} (recipe v{plan.recipe_version})",
            f"platform  {plan.platform} {plan.arch}{'' if plan.platform != 'linux' else ''}",
            f"package   {plan.package} via {plan.manager}",
        ]
        for argv in plan.commands:
            lines.append("run       " + " ".join(argv))
        if plan.healthcheck:
            lines.append("check     " + " ".join(plan.healthcheck))
        if plan.privileged:
            lines.append("privilege sudo -n (passwordless required)")
        if not plan.reproducible:
            lines.append("note      the repository rolls: the installed version is recorded")
        for limitation in plan.limitations:
            lines.append("limit     " + limitation)
        return lines


def render_os(profile: EnvironmentProfile) -> str:
    if profile.os_family is OsFamily.MACOS:
        return "macos"
    return profile.distro or profile.os_family


__all__ = ["Provisioner", "ProvisionError", "render_os", "ProvisionRefusedError"]
