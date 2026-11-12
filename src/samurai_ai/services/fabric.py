"""Where a tool would come from: native, project-local, container, provision, fail.

The resolver is the one place that decides, in a fixed order, so a missing
binary becomes a machine-readable reason instead of a failed command. Native
wins where the hardware matters (GPU, devices), container where isolation does;
provision is a fallback with an approval, never a silent fix. Everything after
the first hit stays a documented reason, not a surprise.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from samurai_ai.domain.environment import (
    Arch,
    EnvironmentDetector,
    EnvironmentProfile,
    OsFamily,
)


class ProvisionReason(StrEnum):
    """Why a path was or was not chosen — stable strings, shown to the model."""

    INSTALLED = "installed"
    MISSING_BINARY = "missing-binary"
    UNSUPPORTED_PLATFORM = "unsupported-platform"
    MISSING_PRIVILEGE = "missing-privilege"
    DOCKER_UNAVAILABLE = "docker-unavailable"
    INCOMPATIBLE_VERSION = "incompatible-version"
    OFFLINE = "offline"
    MANUAL_ONLY = "manual-only"
    CONFLICT = "conflict"


class CapabilityLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_id: str
    path: str = ""
    version: str = ""
    native: bool = False
    container: bool = False
    reason: str = ""


class CapabilitySummary(BaseModel):
    """Short facts for the planner: not the whole catalogue."""

    model_config = ConfigDict(frozen=True)

    captured_at: datetime
    os_family: str
    distro: str
    arch: str
    docker: bool
    sudo: bool
    offline: bool
    tools: tuple[CapabilityLine, ...] = ()
    notes: tuple[str, ...] = ()


def profile_summary(profile: EnvironmentProfile) -> CapabilitySummary:
    """Turn a detected profile into the few facts a planner needs."""
    notes: list[str] = []
    if profile.offline:
        notes.append("offline: nothing can be installed, only present tools run")
    if not profile.docker_daemon:
        notes.append("no docker daemon: container paths are unavailable")
    if not profile.sudo_noninteractive:
        notes.append("no passwordless sudo: package installs will ask the operator")
    tools = tuple(
        CapabilityLine(
            tool_id=name,
            path=probe.path,
            version=probe.version,
            native=probe.present,
            reason="" if probe.present else ProvisionReason.MISSING_BINARY.value,
        )
        for name, probe in sorted(profile.binaries.items())
    )
    return CapabilitySummary(
        captured_at=datetime.now(UTC),
        os_family=profile.os_family,
        distro=profile.distro,
        arch=profile.arch,
        docker=profile.docker_daemon,
        sudo=profile.sudo_noninteractive,
        offline=profile.offline,
        tools=tools,
        notes=tuple(notes),
    )


class Resolution(BaseModel):
    """The chosen path and the machine-readable why."""

    model_config = ConfigDict(frozen=True)

    tool_id: str
    path: str
    reason: ProvisionReason
    detail: str = ""
    executable: str = ""
    script: str = ""
    can_provision: bool = False
    manual_command: str = ""

    @property
    def runnable(self) -> bool:
        return self.reason in (ProvisionReason.INSTALLED,) and bool(self.path)

    @property
    def needs_provision(self) -> bool:
        return self.can_provision


#: platform key the recipes use
def platform_key(profile: EnvironmentProfile) -> str:
    return "macos" if profile.os_family == OsFamily.MACOS else "linux"


def _manual_command(package: str, manager: str) -> str:
    if manager == "brew":
        return f"brew install {package}"
    if manager == "apt":
        return f"sudo apt-get install -y {package}"
    return f"install {package}"


class ToolResolver:
    """Fixed order: native, project-local, container, provision, fail.

    The order is the policy: hardware and devices need the native run, isolation
    needs the container, and provisioning is the fallback that costs an approval
    before a manual instruction.
    """

    def __init__(
        self,
        profile: EnvironmentProfile,
        *,
        project_local_dir: Path | None = None,
    ) -> None:
        self.profile = profile
        self.project_local_dir = project_local_dir

    def resolve(self, tool_id: str, recipe: Any = None) -> Resolution:
        executable = recipe.executable if recipe is not None else ""
        path = which(executable) if executable else ""
        project_local = recipe is not None and recipe.execution.project_local
        if project_local:
            # the tool is the installed script; the interpreter alone proves nothing
            script = self._installed_script(tool_id)
            if script is not None and path:
                return Resolution(
                    tool_id=tool_id,
                    path=path,
                    reason=ProvisionReason.INSTALLED,
                    executable=executable,
                    script=str(script),
                )
        elif recipe is not None and recipe.execution.native and path:
            return Resolution(
                tool_id=tool_id,
                path=path,
                reason=ProvisionReason.INSTALLED,
                executable=executable,
            )
        if recipe is None:
            return Resolution(
                tool_id=tool_id,
                path="",
                reason=ProvisionReason.MANUAL_ONLY,
                detail="no recipe: the tool is not in the reviewed catalogue",
            )
        docker_ok = self.profile.docker_daemon and self.profile.arch in (Arch.ARM64, Arch.X86_64)
        if recipe.execution.container and docker_ok:
            return Resolution(
                tool_id=tool_id,
                path="docker",
                reason=ProvisionReason.INSTALLED,
                detail="container path",
            )
        provisionable, why, manual = self.can_provision(recipe)
        if provisionable:
            return Resolution(
                tool_id=tool_id,
                path="",
                reason=ProvisionReason.MISSING_BINARY,
                detail="installable from the reviewed catalogue",
                executable=executable,
                can_provision=True,
            )
        return Resolution(
            tool_id=tool_id,
            path="",
            reason=why,
            detail=manual,
            executable=executable,
            manual_command=manual,
        )

    def can_provision(self, recipe: Any) -> tuple[bool, ProvisionReason, str]:
        """Why provisioning would fail, or that it would not."""
        platform = platform_key(self.profile)
        spec = recipe.package_for(platform) if hasattr(recipe, "package_for") else None
        if spec is None:
            return (
                False,
                ProvisionReason.UNSUPPORTED_PLATFORM,
                f"no recipe package for {platform}: install it by hand",
            )
        manager_bin = "brew" if spec.manager == "brew" else "apt-get"
        if spec.manager in ("apt", "brew") and manager_bin not in self.profile.package_managers:
            return (
                False,
                ProvisionReason.UNSUPPORTED_PLATFORM,
                f"{manager_bin} is not available here",
            )
        if self.profile.offline:
            return False, ProvisionReason.OFFLINE, "offline: nothing can be installed"
        if spec.sudo and not self.profile.sudo_noninteractive:
            return (
                False,
                ProvisionReason.MISSING_PRIVILEGE,
                _manual_command(spec.package, spec.manager) + " (needs a password)",
            )
        void = recipe.provision
        if void.requires_docker and not self.profile.docker_daemon:
            return False, ProvisionReason.DOCKER_UNAVAILABLE, "docker daemon is not running"
        if void.requires_gpu and not self.profile.gpu:
            return False, ProvisionReason.UNSUPPORTED_PLATFORM, "no usable GPU on this machine"
        if void.min_free_mb and (self.profile.free_bytes or 0) < void.min_free_mb * 1024 * 1024:
            return False, ProvisionReason.UNSUPPORTED_PLATFORM, "not enough free disk space"
        return True, ProvisionReason.MISSING_BINARY, ""

    def _installed_script(self, tool_id: str) -> Path | None:
        if self.project_local_dir is None:
            return None
        candidate = self.project_local_dir / f"{tool_id.replace('.', '-')}.py"
        return candidate if candidate.exists() else None


def detector_for(offline: bool = False) -> EnvironmentDetector:
    return EnvironmentDetector(offline=offline)


def describe_capabilities(offline: bool = False) -> CapabilitySummary:
    """One read-only snapshot; nothing is installed, pulled or elevated."""
    return profile_summary(detector_for(offline).detect())


def which(executable: str) -> str:
    return shutil.which(executable) or ""


__all__ = [
    "CapabilityLine",
    "CapabilitySummary",
    "ProvisionReason",
    "describe_capabilities",
    "detector_for",
    "profile_summary",
    "Resolution",
    "ToolResolver",
    "platform_key",
]
