"""Where a tool would come from: native, project-local, container, provision, relay.

The resolver is the one place that decides, in a fixed order, so a missing
binary becomes a machine-readable reason instead of a failed command. Native
wins where the hardware matters (GPU, devices), container where isolation does;
provision is a fallback with an approval, never a silent fix. Everything after
the first hit stays a documented reason, not a surprise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from samurai_ai.domain.environment import EnvironmentDetector, EnvironmentProfile


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


def detector_for(offline: bool = False) -> EnvironmentDetector:
    return EnvironmentDetector(offline=offline)


def describe_capabilities(offline: bool = False) -> CapabilitySummary:
    """One read-only snapshot; nothing is installed, pulled or elevated."""
    return profile_summary(detector_for(offline).detect())


__all__ = [
    "CapabilityLine",
    "CapabilitySummary",
    "ProvisionReason",
    "describe_capabilities",
    "detector_for",
    "profile_summary",
]
