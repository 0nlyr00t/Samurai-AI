"""Docker sandbox runner for governed, networkless tool execution.

Builds a ``docker run`` argv without a shell and executes it through the
managed ProcessManager. The image must be pinned by digest and already
present locally — auto-pull is forbidden. Sandbox profiles are static and
must carry a valid HMAC signature derived from the vault master key.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

from samurai_ai.domain.execution import ToolValidationError
from samurai_ai.domain.sandbox import (
    SandboxError,
    SandboxProfile,
    SandboxSignatureError,
    sign_profiles,
    verify_profiles,
)
from samurai_ai.execution.process import ProcessManager, ProcessResult

DOCKER_EXECUTABLE = "docker"
_DOCKER_PROBE_TIMEOUT = 5.0
_TIMEOUT_GRACE_SECONDS = 10.0

SANDBOX_PROFILES: tuple[SandboxProfile, ...] = (
    SandboxProfile(
        id="sandbox.default@v1",
        description="Networkless busybox sandbox for trivial lookups and text processing",
        image=(
            "busybox@sha256:"
            "bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
        ),
        allowed_executables=("echo", "cat", "grep", "wc", "head", "sort", "uniq"),
        cpu_limit="1.0",
        memory_mb=128,
        pids_limit=64,
        timeout_seconds=30,
        max_output_bytes=65536,
        network_mode="none",
    ),
)


def get_profile(profile_id: str) -> SandboxProfile:
    for profile in SANDBOX_PROFILES:
        if profile.id == profile_id:
            return profile
    raise ToolValidationError(
        f"unknown sandbox profile: {profile_id!r} "
        f"(available: {', '.join(profile.id for profile in SANDBOX_PROFILES)})"
    )


class DockerSandboxRunner:
    def __init__(
        self,
        manager: ProcessManager,
        signature_path: Path,
        master_key_loader: Callable[[], bytes],
    ) -> None:
        self.manager = manager
        self.signature_path = signature_path
        self.master_key_loader = master_key_loader

    def build_argv(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
    ) -> list[str]:
        if executable not in profile.allowed_executables:
            raise ToolValidationError(
                f"executable {executable!r} is not in sandbox profile {profile.id} "
                f"allowlist ({', '.join(profile.allowed_executables)})"
            )
        if any("\x00" in argument for argument in args):
            raise ToolValidationError("sandbox args cannot contain NUL bytes")
        return [
            DOCKER_EXECUTABLE,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--cpus",
            profile.cpu_limit,
            "--memory",
            f"{profile.memory_mb}m",
            "--pids-limit",
            str(profile.pids_limit),
            "--name",
            f"samurai-{run_id}",
            profile.image,
            executable,
            *args,
        ]

    def verify_allowlist_signature(self) -> None:
        if not self.signature_path.exists():
            raise SandboxSignatureError(
                f"sandbox allowlist signature is missing: {self.signature_path}; "
                "create it with 'samurai sandbox sign'"
            )
        signature = self.signature_path.read_text(encoding="utf-8")
        if not verify_profiles(self.master_key_loader(), SANDBOX_PROFILES, signature):
            raise SandboxSignatureError(
                "sandbox allowlist signature is invalid; "
                "re-create it with 'samurai sandbox sign'"
            )

    async def docker_available(self) -> bool:
        try:
            result = await self.manager.run(
                (DOCKER_EXECUTABLE, "version", "--format", "{{.Server.Version}}"),
                timeout_seconds=_DOCKER_PROBE_TIMEOUT,
            )
        except Exception:
            return False
        return result.exit_code == 0

    async def image_present(self, image: str) -> bool:
        try:
            result = await self.manager.run(
                (DOCKER_EXECUTABLE, "image", "inspect", image),
                timeout_seconds=_DOCKER_PROBE_TIMEOUT,
            )
        except Exception:
            return False
        return result.exit_code == 0

    async def preflight(self, profile: SandboxProfile) -> None:
        self.verify_allowlist_signature()
        if not await self.docker_available():
            raise SandboxError(
                "docker is not available; sandbox.exec requires a running Docker daemon"
            )
        if not await self.image_present(profile.image):
            raise SandboxError(
                f"sandbox image is not present locally: {profile.image}; "
                "pull it manually as a trusted operator action (auto-pull is disabled)"
            )

    def run(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
    ) -> ProcessResult:
        return asyncio.run(self._run(profile, run_id, executable, args))

    async def _run(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
    ) -> ProcessResult:
        argv = self.build_argv(profile, run_id, executable, args)
        await self.preflight(profile)
        result = await self.manager.run(
            argv, timeout_seconds=profile.timeout_seconds + _TIMEOUT_GRACE_SECONDS
        )
        return self._enforce_output_limit(profile, result)

    @staticmethod
    def _enforce_output_limit(profile: SandboxProfile, result: ProcessResult) -> ProcessResult:
        truncated = result.truncated
        stdout = result.stdout.encode("utf-8")[: profile.max_output_bytes].decode(
            "utf-8", errors="ignore"
        )
        stderr = result.stderr.encode("utf-8")[: profile.max_output_bytes].decode(
            "utf-8", errors="ignore"
        )
        if len(result.stdout.encode("utf-8")) > profile.max_output_bytes:
            truncated = True
        return ProcessResult(
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            truncated=truncated,
        )


def sign_allowlist(signature_path: Path, master_key: bytes) -> str:
    signature = sign_profiles(master_key, SANDBOX_PROFILES)
    signature_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(signature_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, (signature + "\n").encode("ascii"))
    finally:
        os.close(descriptor)
    return signature


__all__ = [
    "DOCKER_EXECUTABLE",
    "SANDBOX_PROFILES",
    "DockerSandboxRunner",
    "get_profile",
    "sign_allowlist",
]
