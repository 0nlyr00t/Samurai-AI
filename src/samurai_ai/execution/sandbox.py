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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from samurai_ai.domain.execution import ToolValidationError
from samurai_ai.domain.sandbox import (
    SandboxError,
    SandboxProfile,
    SandboxSignatureError,
    sign_profiles,
    verify_profiles,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.egress import (
    DEFAULT_ALLOWED_PORTS,
    EgressProxy,
    generate_proxy_token,
)
from samurai_ai.execution.process import ProcessManager, ProcessResult

DOCKER_EXECUTABLE = "docker"
_DOCKER_PROBE_TIMEOUT = 5.0
_TIMEOUT_GRACE_SECONDS = 10.0


@dataclass(frozen=True)
class SandboxRunOutcome:
    result: ProcessResult
    proxy_token: str | None = None
    egress_events: tuple[dict[str, Any], ...] = ()

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
    SandboxProfile(
        id="sandbox.scoped@v1",
        description="Busybox sandbox whose only egress path is the scoped, "
        "token-authenticated host egress proxy",
        image=(
            "busybox@sha256:"
            "bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
        ),
        allowed_executables=("echo", "cat", "grep", "wc", "head", "sort", "uniq", "wget"),
        cpu_limit="1.0",
        memory_mb=128,
        pids_limit=64,
        timeout_seconds=30,
        max_output_bytes=65536,
        network_mode="scoped",
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
        egress_allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
    ) -> None:
        self.manager = manager
        self.signature_path = signature_path
        self.master_key_loader = master_key_loader
        self.egress_allowed_ports = egress_allowed_ports

    def validate_invocation(
        self, profile: SandboxProfile, executable: str, args: list[str]
    ) -> None:
        if executable not in profile.allowed_executables:
            raise ToolValidationError(
                f"executable {executable!r} is not in sandbox profile {profile.id} "
                f"allowlist ({', '.join(profile.allowed_executables)})"
            )
        if any("\x00" in argument for argument in args):
            raise ToolValidationError("sandbox args cannot contain NUL bytes")

    def build_argv(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
        proxy_url: str | None = None,
    ) -> list[str]:
        self.validate_invocation(profile, executable, args)
        argv = [
            DOCKER_EXECUTABLE,
            "run",
            "--rm",
        ]
        if profile.network_mode == "scoped":
            if proxy_url is None:
                raise SandboxError(
                    f"sandbox profile {profile.id} requires a scoped egress proxy"
                )
            argv += [
                "--add-host",
                "host.docker.internal:host-gateway",
                "--env",
                f"HTTP_PROXY={proxy_url}",
                "--env",
                f"HTTPS_PROXY={proxy_url}",
                "--env",
                f"http_proxy={proxy_url}",
                "--env",
                f"https_proxy={proxy_url}",
                "--env",
                "NO_PROXY=",
            ]
        else:
            argv += ["--network", "none"]
        argv += [
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
        return argv

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
        scope: ScopeContract | None = None,
    ) -> SandboxRunOutcome:
        return asyncio.run(self._run(profile, run_id, executable, args, scope))

    async def _run(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
        scope: ScopeContract | None,
    ) -> SandboxRunOutcome:
        proxy: EgressProxy | None = None
        proxy_url: str | None = None
        if profile.network_mode == "scoped":
            if scope is None:
                raise SandboxError(
                    f"sandbox profile {profile.id} requires the operation scope"
                )
            token = generate_proxy_token()
            proxy = EgressProxy(
                scope, token, allowed_ports=self.egress_allowed_ports
            )
            _, proxy_port = await proxy.start()
            proxy_url = f"http://samurai:{token}@host.docker.internal:{proxy_port}"
        argv = self.build_argv(profile, run_id, executable, args, proxy_url)
        await self.preflight(profile)
        try:
            result = await self.manager.run(
                argv, timeout_seconds=profile.timeout_seconds + _TIMEOUT_GRACE_SECONDS
            )
        finally:
            if proxy is not None:
                await proxy.stop()
        outcome = SandboxRunOutcome(
            result=self._enforce_output_limit(profile, result),
            proxy_token=proxy.token if proxy is not None else None,
            egress_events=tuple(
                {
                    "host": event.host,
                    "port": event.port,
                    "verdict": event.verdict,
                    "bytes_sent": event.bytes_sent,
                    "bytes_received": event.bytes_received,
                }
                for event in (proxy.events if proxy is not None else [])
            ),
        )
        return outcome

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
    "SandboxRunOutcome",
    "get_profile",
    "sign_allowlist",
]
