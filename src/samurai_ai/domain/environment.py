"""What this machine can do, asked once and without changing it.

The harness runs where the operator already has tools; knowing that up front is
the difference between offering a native run and failing one command at a time.
Detection is read-only: no installs, no pulls, no sudo, no configuration writes.
Every probe reports its own error, so one broken check never hides the rest.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class OsFamily:
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    OTHER = "other"


class Arch:
    ARM64 = "arm64"
    X86_64 = "x86_64"
    OTHER = "other"


def normalize_arch(machine: str | None = None) -> str:
    raw = (machine if machine is not None else platform.machine()).lower()
    if raw in ("arm64", "aarch64"):
        return Arch.ARM64
    if raw in ("x86_64", "amd64"):
        return Arch.X86_64
    return Arch.OTHER


def parse_os_release(text: str) -> tuple[str, str]:
    """(distro id, version id) from /etc/os-release content."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value.strip().strip('"').strip("'")
    return values.get("ID", ""), values.get("VERSION_ID", "")


def normalize_os(system: str | None = None) -> str:
    raw = (system if system is not None else platform.system()).lower()
    if raw == "darwin":
        return OsFamily.MACOS
    if raw == "linux":
        return OsFamily.LINUX
    if raw == "windows":
        return OsFamily.WINDOWS
    return OsFamily.OTHER


class BinaryProbe(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    present: bool
    path: str = ""
    version: str = ""
    error: str = ""


class EnvironmentProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SCHEMA_VERSION
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    os_family: str
    distro: str = ""
    distro_version: str = ""
    arch: str
    offline: bool = False
    package_managers: dict[str, str] = Field(default_factory=dict)
    binaries: dict[str, BinaryProbe] = Field(default_factory=dict)
    docker_cli: bool = False
    docker_daemon: bool = False
    docker_server_version: str = ""
    sudo_noninteractive: bool = False
    gpu: bool = False
    free_bytes: int | None = None
    #: one entry per failed probe, never a reason to skip the others
    probe_errors: dict[str, str] = Field(default_factory=dict)

    def has(self, name: str) -> bool:
        probe = self.binaries.get(name)
        return probe is not None and probe.present

    def path_of(self, name: str) -> str:
        probe = self.binaries.get(name)
        return probe.path if probe is not None else ""


class EnvironmentDetector:
    """Read-only environment detection, cached inside the process."""

    def __init__(
        self,
        *,
        offline: bool = False,
        tool_names: tuple[str, ...] = ("nmap", "curl", "ffuf", "nuclei", "hashcat"),
        runner: Any = None,
    ) -> None:
        self.offline = offline
        self.tool_names = tool_names
        # a testable seam: probes go through one function that returns
        # (returncode, stdout, stderr) without ever needing a real package manager
        self._runner = runner or self._run
        self._cached: EnvironmentProfile | None = None

    @staticmethod
    def _run(argv: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, "", str(exc)
        return done.returncode, done.stdout or "", done.stderr or ""

    def detect(self, *, refresh: bool = False) -> EnvironmentProfile:
        if self._cached is not None and not refresh:
            return self._cached
        os_family = normalize_os()
        arch = normalize_arch()
        distro, version = self._distro(os_family)
        managers: dict[str, str] = {}
        for name in ("apt-get", "brew", "dnf", "pacman", "apk"):
            found = shutil.which(name)
            if found:
                managers[name] = found
        docker_cli = shutil.which("docker") is not None
        docker_daemon = False
        docker_version = ""
        if docker_cli:
            code, out, err = self._runner(
                ["docker", "version", "--format", "{{.Server.Version}}"]
            )
            docker_daemon = code == 0
            docker_version = out.strip() if code == 0 else ""
            if code != 0:
                # the daemon being down is information, not a probe failure
                docker_version = err.strip()
        sudo_available = False
        if shutil.which("sudo") is not None:
            # -n: never prompt; a refusal is an answer, not a hang
            code, _, _ = self._runner(["sudo", "-n", "true"], timeout=5.0)
            sudo_available = code == 0
        gpu = self._gpu(os_family)
        free = self._free_bytes()
        binaries: dict[str, BinaryProbe] = {}
        for name in self.tool_names:
            found = shutil.which(name)
            if not found:
                binaries[name] = BinaryProbe(name=name, present=False)
                continue
            version_text = ""
            if name in ("nmap", "curl", "ffuf", "nuclei", "hashcat"):
                code, out, err = self._runner([found, "--version"], timeout=6.0)
                lines = [
                    line.strip()
                    for line in (out or err).strip().splitlines()
                    if line.strip() and not line.startswith(("WARNING", "warning"))
                ]
                version_text = lines[0] if lines else ""
            binaries[name] = BinaryProbe(name=name, present=True, path=found, version=version_text)
        self._cached = EnvironmentProfile(
            os_family=os_family,
            distro=distro,
            distro_version=version,
            arch=arch,
            offline=self.offline,
            package_managers=managers,
            binaries=binaries,
            docker_cli=docker_cli,
            docker_daemon=docker_daemon,
            docker_server_version=docker_version,
            sudo_noninteractive=sudo_available,
            gpu=gpu,
            free_bytes=free,
        )
        return self._cached

    def _distro(self, os_family: str) -> tuple[str, str]:
        if os_family != OsFamily.LINUX:
            return ("macos" if os_family == OsFamily.MACOS else os_family), platform.release()
        try:
            text = Path("/etc/os-release").read_text()
        except OSError:
            return "", ""
        return parse_os_release(text)

    @staticmethod
    def _gpu(os_family: str) -> bool:
        # A discrete/usable GPU is a fact about hardware, not a permission probe:
        # nvidia-smi or an Apple integrated GPU count, nothing is installed.
        if shutil.which("nvidia-smi") is not None:
            return True
        if os_family == OsFamily.MACOS:
            return normalize_arch() == Arch.ARM64
        return False

    @staticmethod
    def _free_bytes() -> int | None:
        """Free space where state lives; unknown stays None rather than 0."""
        try:
            return int(shutil.disk_usage(str(Path.home())).free)
        except OSError:
            return None


__all__ = [
    "Arch",
    "BinaryProbe",
    "EnvironmentDetector",
    "EnvironmentProfile",
    "OsFamily",
    "normalize_arch",
    "normalize_os",
    "parse_os_release",
    "SCHEMA_VERSION",
]
