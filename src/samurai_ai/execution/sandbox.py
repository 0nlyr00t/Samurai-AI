"""Docker sandbox runner for governed, networkless tool execution.

Builds a ``docker run`` argv without a shell and executes it through the
managed ProcessManager. The image must be pinned by digest and already
present locally — auto-pull is forbidden. Sandbox profiles are static and
must carry a valid HMAC signature derived from the vault master key.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from samurai_ai.domain.execution import ToolValidationError
from samurai_ai.domain.sandbox import (
    MAX_ENV_NAMES,
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
    scope_ports,
)
from samurai_ai.execution.process import ProcessManager, ProcessResult

DOCKER_EXECUTABLE = "docker"
#: Escape hatch for VPN subnets a bridge cannot route to: host networking skips
#: our proxy, so the adapter's own scope check is what stands in front.
NETWORK_OVERRIDE_ENV = "SAMURAI_SANDBOX_NETWORK"
_DOCKER_PROBE_TIMEOUT = 5.0
_TIMEOUT_GRACE_SECONDS = 10.0
_MAX_SECRET_ENV_BYTES = 8192


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
        # The channel's own test target: `cat /proc/self/environ` in the opt-in
        # docker test proves a value reaches the container without entering argv.
        env_names=("SAMURAI_TEST_SECRET",),
        # busybox wget/json tools read HTTP(S)_PROXY; no flag exists here
        proxy_via_env=True,
    ),
    SandboxProfile(
        id="sandbox.httpx@v1",
        description="httpx prober (projectdiscovery/httpx) behind the scoped "
        "egress proxy; image digest pinned at adoption time",
        image=(
            "projectdiscovery/httpx@sha256:"
            "f8275a7c6aaec187249df9eaf4ad9196f4677adfc6a51a47b4aaa1d4db4f8ffc"
        ),
        allowed_executables=("httpx",),
        cpu_limit="1.0",
        memory_mb=512,
        pids_limit=128,
        timeout_seconds=300,
        max_output_bytes=262144,
        network_mode="scoped",
        # reads HTTP(S)_PROXY from the environment; no flag for it
        proxy_via_env=True,
    ),
    SandboxProfile(
        id="sandbox.nuclei@v1",
        description="nuclei scanner (projectdiscovery/nuclei) behind the scoped "
        "egress proxy; image digest pinned at adoption time",
        image=(
            "projectdiscovery/nuclei@sha256:"
            "582d5546902e67052097cb2d07296c642d50a1afc5e44623cb038845df9a32eb"
        ),
        allowed_executables=("nuclei",),
        cpu_limit="2.0",
        memory_mb=1024,
        pids_limit=256,
        timeout_seconds=600,
        max_output_bytes=524288,
        network_mode="scoped",
        proxy_flag="-proxy",
    ),
    SandboxProfile(
        id="sandbox.subfinder@v1",
        description="Passive subdomain enumeration (projectdiscovery/subfinder) "
        "behind the scoped egress proxy; API keys arrive only by name",
        image=(
            "projectdiscovery/subfinder@sha256:"
            "2f5ddea311db2a8b848e40f1b669fc655d0eb78b74a74ead93d3850e372b12bf"
        ),
        allowed_executables=("subfinder",),
        cpu_limit="1.0",
        memory_mb=512,
        pids_limit=128,
        timeout_seconds=300,
        max_output_bytes=262144,
        network_mode="scoped",
        proxy_flag="-proxy",
        # Passive sources are third parties, not targets: their API hosts are
        # part of the signed profile and are allowed besides the operation
        # scope. A miss shows up as a denied host in the egress audit.
        egress_hosts=(
            # keyless sources
            "crt.sh",
            "anubisdb.com",
            "web.archive.org",
            "archive.org",
            "index.commoncrawl.org",
            "*.commoncrawl.org",
            "www.sitedossier.com",
            "ci-www.threatcrowd.org",
            "rapiddns.io",
            "api.hackertarget.com",
            "api.subdomain.center",
            # keyed sources
            "api.shodan.io",
            "www.virustotal.com",
            "api.securitytrails.com",
            "api.github.com",
            "api.platform.censys.io",
            "urlscan.io",
            "dns.projectdiscovery.io",
            "api.c99.nl",
            "api.certspotter.com",
            "api.dnsdumpster.com",
            "quake.360.net",
            "2.intelx.io",
            "api.digitalyama.com",
            "fofa.info",
            "api.zoomeye.org",
            "windvane.lichoin.com",
        ),
        # Axes the provider keys: subfinder reads "{SOURCE}_API_KEY" from env.
        env_names=(
            "SHODAN_API_KEY",
            "VIRUSTOTAL_API_KEY",
            "SECURITYTRAILS_API_KEY",
            "GITHUB_API_KEY",
            "CENSYS_API_KEY",
            "URLSCAN_API_KEY",
            "CHAOS_API_KEY",
            "C99_API_KEY",
            "CERTSPOTTER_API_KEY",
            "DNSDUMPSTER_API_KEY",
            "QUAKE_API_KEY",
            "INTELX_API_KEY",
            "DIGITALYAMA_API_KEY",
            "FOFA_API_KEY",
            "ZOOMEYEAPI_API_KEY",
            "WINDVANE_API_KEY",
        ),
    ),
    SandboxProfile(
        id="sandbox.dnsx@v1",
        description="DNS resolver (projectdiscovery/dnsx) pinned to a DoH "
        "resolver and reached only through the SOCKS5 egress entrance",
        image=(
            "projectdiscovery/dnsx@sha256:"
            "8fced0bedf3655e06c9f1805f6277d60d4988a4b13209e24b3d7cde7baa656ec"
        ),
        allowed_executables=("dnsx",),
        cpu_limit="1.0",
        memory_mb=256,
        pids_limit=64,
        timeout_seconds=180,
        max_output_bytes=262144,
        network_mode="scoped",
        proxy_flag="-proxy",
        proxy_scheme="socks5",
        # The resolver is part of the signature: the model cannot point the
        # lookups at a DoH endpoint of its choosing.
        resolver_url="doh:https://cloudflare-dns.com/dns-query",
        egress_hosts=("cloudflare-dns.com",),
    ),
    SandboxProfile(
        id="sandbox.nmap@v1",
        description="Port scanner (nmap) on plain bridge networking: a direct "
        "class where only the adapter's scope check stands in front",
        image=(
            "instrumentisto/nmap@sha256:"
            "96f6ed194519b62421a1a1c57809e65a7f94d2aa1c8c25676f247e5e148c0827"
        ),
        allowed_executables=("nmap",),
        cpu_limit="2.0",
        memory_mb=512,
        pids_limit=256,
        timeout_seconds=600,
        max_output_bytes=524288,
        # nmap's own --proxies covers only -sV/NSE and takes no credentials, and
        # port scanning ignores it entirely — so the proxy is left out.
        network_mode="direct",
    ),
    SandboxProfile(
        id="sandbox.ffuf@v1",
        description="Path fuzzer (ffuf) behind the scoped egress proxy; the "
        "wordlist arrives over stdin, so no host mount is needed",
        image=(
            "secsi/ffuf@sha256:"
            "ee0390afbb0471c18cb6c0407191b424d86dc4ab30c04fb93b3908751a33bc5e"
        ),
        allowed_executables=("ffuf",),
        cpu_limit="1.0",
        memory_mb=256,
        pids_limit=128,
        timeout_seconds=300,
        max_output_bytes=262144,
        stall_seconds=300,
        network_mode="scoped",
        proxy_flag="-x",
    ),
    SandboxProfile(
        id="sandbox.remote@v1",
        description="Remote administration belt (ssh, smbclient, adb, nmap, "
        "curl, jq) behind the scoped proxy",
        # built from packaging/sandbox/remote and published by ci as a
        # multi-arch index; the pin comes as a pull request, never by hand
        image="ghcr.io/0nlyr00t/samurai-remote@sha256:a7ca49fefb8629850595fbb668be123fa2537d045a710efff110f94cab396c60",
        allowed_executables=(
            "ssh",
            "smbclient",
            "adb",
            "nmap",
            "curl",
            "jq",
            "socat",
            # the remote drivers are python: they own the tunnel and hand the
            # endpoint to ssh, smbclient, xfreerdp or adb
            "python3",
        ),
        cpu_limit="2.0",
        memory_mb=1024,
        pids_limit=512,
        timeout_seconds=900,
        max_output_bytes=524288,
        stall_seconds=300,
        network_mode="scoped",
        # reads HTTP(S)_PROXY from the environment; no flag for it
        proxy_via_env=True,
    ),
    SandboxProfile(
        id="sandbox.exploit@v1",
        description="Exploit belt (gcc, gdb, checksec, ROPgadget, pwntools, "
        "impacket) with the scoped proxy for the run step",
        image="ghcr.io/0nlyr00t/samurai-exploit@sha256:b68e8b04f3d629da3985bcfcf1fc3245e907302fc0d4c44a6425d1b5790e5dee",
        allowed_executables=(
            "gcc",
            "gdb",
            "objdump",
            "readelf",
            "checksec",
            "ROPgadget",
            "python3",
            "socat",
            "file",
        ),
        cpu_limit="2.0",
        memory_mb=1024,
        pids_limit=512,
        timeout_seconds=900,
        max_output_bytes=524288,
        stall_seconds=300,
        network_mode="scoped",
        # reads HTTP(S)_PROXY from the environment; no flag for it
        proxy_via_env=True,
    ),
    SandboxProfile(
        id="sandbox.camoufox@v1",
        description="Anti-detect Firefox (camoufox) driven by an inline python "
        "script; the only network path is the scoped proxy",
        image=(
            "etriasnl/camoufox@sha256:"
            "f9d586d5e4c9f6a826afa8479a61dbbd42eedb69d522718fbe2ce64fc008e5a9"
        ),
        allowed_executables=("python3",),
        cpu_limit="2.0",
        memory_mb=1024,
        pids_limit=512,
        timeout_seconds=600,
        max_output_bytes=524288,
        stall_seconds=300,
        network_mode="scoped",
        # reads HTTP(S)_PROXY from the environment; no flag for it
        proxy_via_env=True,
        # the browser build lives under /root/.cache (mode 700)
        user="0:0",
        # camoufox checks its release and does a geoip lookup when a proxy is set
        egress_hosts=(
            "html.duckduckgo.com",
            "duckduckgo.com",
            "raw.githubusercontent.com",
            "api.github.com",
            "github.com",
        ),
    ),
    SandboxProfile(
        id="sandbox.pdf@v1",
        description="Print report HTML to PDF in pinned headless Chromium; the "
        "document is local, so nothing leaves the container",
        # amd64 only upstream: on arm64 docker runs it under emulation
        image=(
            "zenika/alpine-chrome@sha256:"
            "d3f2d5a5fe4b57ac4c9d3478feb5a077783a053a2aceb4188d9257a55e4b8ab0"
        ),
        allowed_executables=("sh", "chromium-browser"),
        cpu_limit="2.0",
        memory_mb=1024,
        pids_limit=256,
        timeout_seconds=300,
        # 3 MiB of PDF comes back as ~4 MiB of base64 on stdout
        max_output_bytes=4 * 1024 * 1024,
        network_mode="none",
        user="65534:65534",
    ),
)


def installed_profile(manifest_id: str) -> SandboxProfile | None:
    """The profile of an installed manifest, if one is installed."""
    from samurai_ai.config import tools_directory
    from samurai_ai.services.manifests import ManifestStore

    for manifest in ManifestStore(tools_directory()).load_all():
        if manifest.id == manifest_id:
            return manifest.profile()
    return None


def all_profiles() -> tuple[SandboxProfile, ...]:
    """Static profiles plus one per installed manifest.

    Manifests add profiles at runtime, so signing and lookup must see the union
    — otherwise a manifest tool could never pass the signature check.
    """
    from samurai_ai.config import profiles_directory, tools_directory
    from samurai_ai.services.manifests import ManifestStore

    extra = tuple(
        manifest.profile() for manifest in ManifestStore(tools_directory()).load_all()
    )
    return (*SANDBOX_PROFILES, *extra, *load_profile_files(profiles_directory()))


def load_profile_files(directory: Path) -> tuple[SandboxProfile, ...]:
    """Profiles installed as files (plugin packages); a broken one is skipped."""
    if not directory.exists():
        return ()
    profiles: list[SandboxProfile] = []
    for path in sorted(directory.glob("*.json")):
        try:
            profiles.append(SandboxProfile.model_validate_json(path.read_text()))
        except Exception:
            continue
    return tuple(profiles)


def get_profile(profile_id: str) -> SandboxProfile:
    for profile in all_profiles():
        if profile.id == profile_id:
            return profile
    raise ToolValidationError(
        f"unknown sandbox profile: {profile_id!r} "
        f"(available: {', '.join(profile.id for profile in all_profiles())})"
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
        # Sidecar with the last automatic re-sign: reason and time survive the
        # stderr scroll-back and `sandbox profiles` shows them.
        self.resign_journal_path = signature_path.with_suffix(".reason")
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

    def validate_secret_env(
        self, profile: SandboxProfile, secret_env: dict[str, str]
    ) -> None:
        """Secret values stay out of argv: only declared names may be passed."""
        if len(secret_env) > MAX_ENV_NAMES:
            raise ToolValidationError(
                f"at most {MAX_ENV_NAMES} secret env variables per run"
            )
        for name, value in secret_env.items():
            if name not in profile.env_names:
                raise ToolValidationError(
                    f"env variable {name!r} is not declared by sandbox profile "
                    f"{profile.id} ({', '.join(profile.env_names) or 'none'})"
                )
            if not value:
                raise ToolValidationError(f"secret env variable {name!r} is empty")
            if "\n" in value or "\r" in value:
                raise ToolValidationError(
                    f"secret env variable {name!r} must be a single line"
                )
            if len(value.encode("utf-8")) > _MAX_SECRET_ENV_BYTES:
                raise ToolValidationError(
                    f"secret env variable {name!r} exceeds "
                    f"{_MAX_SECRET_ENV_BYTES} bytes"
                )

    def build_argv(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
        proxy_url: str | None = None,
        env: dict[str, str] | None = None,
        secret_env: dict[str, str] | None = None,
        hosts: dict[str, str] | None = None,
        network: str | None = None,
    ) -> list[str]:
        self.validate_invocation(profile, executable, args)
        mode = network or _forced_network() or profile.network_mode
        if mode not in ("none", "scoped", "direct", "host"):
            raise SandboxError(f"unknown network mode {mode!r}")
        if secret_env:
            self.validate_secret_env(profile, secret_env)
        argv = [
            DOCKER_EXECUTABLE,
            "run",
            "--rm",
            # docker only forwards our stdin into the container with -i; the
            # process stdin is always closed, so a tool sees EOF either way.
            "--interactive",
        ]
        # an aliased vhost must resolve inside the container too, or the tool
        # gets a name the sandbox DNS does not know
        for name, address in sorted((hosts or {}).items()):
            argv += ["--add-host", f"{name}:{address}"]
        if mode == "scoped":
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
        elif mode in ("none", "host"):
            argv += ["--network", mode]
        elif mode == "direct":
            # direct keeps bridge networking as-is and no proxy of ours in the
            # path, but a host-loopback target still has to be nameable
            argv += ["--add-host", "host.docker.internal:host-gateway"]
        for name, value in (env or {}).items():
            argv += ["--env", f"{name}={value}"]
        for name in (secret_env or {}):
            # Value comes from the docker CLI environment, so it stays out of argv.
            argv += ["--env", name]
        argv += [
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            profile.user,
            "--cpus",
            profile.cpu_limit,
            "--memory",
            f"{profile.memory_mb}m",
            "--pids-limit",
            str(profile.pids_limit),
            "--name",
            f"samurai-{run_id}",
            # The executable rides on --entrypoint: the images set their own
            # entrypoint, so repeating the name as a command would double it.
            "--entrypoint",
            executable,
            profile.image,
            *args,
        ]
        return argv

    def allowlist_status(self) -> str:
        """``signed`` / ``unsigned`` / ``invalid`` — without writing anything."""
        if not self.signature_path.exists():
            return "unsigned"
        try:
            master_key = self.master_key_loader()
        except Exception:
            return "unknown"
        signature = self.signature_path.read_text(encoding="utf-8")
        return (
            "signed"
            if verify_profiles(master_key, all_profiles(), signature)
            else "invalid"
        )

    def verify_allowlist_signature(self) -> str | None:
        """Keep the allowlist signed; re-sign on drift and say so.

        The signature lives next to the state, so its key is as reachable as the
        project code is: what it buys is pinned digests in one artifact and a
        visible drift, not authenticity against a local writer. Re-signing on
        drift therefore beats refusing on every upgrade — as long as it is
        announced and recorded.
        """
        status = self.allowlist_status()
        if status == "signed":
            return None
        if status == "unknown":
            raise SandboxSignatureError(
                "sandbox allowlist signature cannot be checked: the vault master "
                "key is unavailable"
            )
        master_key = self.master_key_loader()
        sign_allowlist(self.signature_path, master_key)
        self._record_resign(status)
        return (
            f"allowlist signature was {status}; re-signed {len(all_profiles())} "
            f"profile(s) after a profile change"
        )

    def _record_resign(self, reason: str) -> None:
        payload = {
            "resigned_at": datetime.now(UTC).isoformat(),
            "reason": reason,
            "profiles": len(SANDBOX_PROFILES),
        }
        descriptor = os.open(
            self.resign_journal_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(descriptor, (json.dumps(payload) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)

    def resign_record(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.resign_journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

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

    async def preflight(self, profile: SandboxProfile) -> str | None:
        notice = self.verify_allowlist_signature()
        if not await self.docker_available():
            raise SandboxError(
                "docker is not available; sandbox.exec requires a running Docker daemon"
            )
        if not await self.image_present(profile.image):
            hint = ""
            if "ghcr.io/" in profile.image:
                # the belt images are still private: an anonymous pull is denied,
                # and "not present locally" alone sends the operator round in circles
                hint = (
                    " — the GHCR belt packages are private, so run "
                    "`docker login ghcr.io` once with a token that has read:packages"
                )
            raise SandboxError(
                f"sandbox image is not present locally: {profile.image}; "
                "pull it manually as a trusted operator action (auto-pull is disabled)"
                + hint
            )
        return notice

    def run(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
        scope: ScopeContract | None = None,
        *,
        env: dict[str, str] | None = None,
        secret_env: dict[str, str] | None = None,
        stdin_data: bytes | None = None,
        network: str | None = None,
    ) -> SandboxRunOutcome:
        return asyncio.run(
            self._run(
                profile, run_id, executable, args, scope, env, secret_env,
                stdin_data, network,
            )
        )

    async def run_async(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
        scope: ScopeContract | None,
        *,
        env: dict[str, str] | None = None,
        secret_env: dict[str, str] | None = None,
        stdin_data: bytes | None = None,
        network: str | None = None,
    ) -> SandboxRunOutcome:
        """Same as `run`, for callers that already own an event loop.

        A tunnel or a watcher can then live in the same loop as the process.
        """
        return await self._run(
            profile, run_id, executable, args, scope, env, secret_env,
            stdin_data, network,
        )

    async def _run(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
        scope: ScopeContract | None,
        env: dict[str, str] | None = None,
        secret_env: dict[str, str] | None = None,
        stdin_data: bytes | None = None,
        network: str | None = None,
    ) -> SandboxRunOutcome:
        proxy: EgressProxy | None = None
        proxy_url: str | None = None
        mode = network or _forced_network() or profile.network_mode
        if mode == "scoped":
            if scope is None:
                raise SandboxError(
                    f"sandbox profile {profile.id} requires the operation scope"
                )
            token = generate_proxy_token()
            proxy = EgressProxy(
                scope,
                token,
                allowed_ports=self.egress_allowed_ports | scope_ports(scope),
                extra_hosts=frozenset(profile.egress_hosts),
            )
            _, proxy_port = await proxy.start()
            proxy_url = (
                f"{profile.proxy_scheme}://samurai:{token}"
                f"@host.docker.internal:{proxy_port}"
            )
        if proxy_url is not None and profile.proxy_flag is not None:
            args = [*args, profile.proxy_flag, proxy_url]
        argv = self.build_argv(
            profile,
            run_id,
            executable,
            args,
            proxy_url,
            env,
            secret_env,
            hosts=scope.hosts if scope is not None else None,
            network=network,
        )
        await self.preflight(profile)
        # the watcher is per profile: nmap and a browser legitimately go quiet
        previous_stall = self.manager.stall_seconds
        if getattr(self, "honor_profile_stall", True):
            self.manager.stall_seconds = float(profile.stall_seconds)
        child_env = {**(env or {}), **(secret_env or {})} or None
        extra: dict[str, Any] = {}
        if child_env:
            extra["env"] = child_env
        if stdin_data is not None:
            extra["stdin_data"] = stdin_data
        try:
            result = await self.manager.run(
                argv,
                timeout_seconds=profile.timeout_seconds + _TIMEOUT_GRACE_SECONDS,
                **extra,
            )
        finally:
            self.manager.stall_seconds = previous_stall
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
            # a stall (no output within the window) is why a hung ssh dies: keep
            # the flag so the adapter can surface it as a timeout, not a silent exit
            stalled=result.stalled,
        )


def sign_allowlist(signature_path: Path, master_key: bytes) -> str:
    signature = sign_profiles(master_key, all_profiles())
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
    "all_profiles",
    "installed_profile",
    "DockerSandboxRunner",
    "SandboxRunOutcome",
    "get_profile",
    "sign_allowlist",
]


def _forced_network() -> str | None:
    """Operator override, for networks a bridge container cannot route to."""
    value = os.environ.get(NETWORK_OVERRIDE_ENV, "").strip()
    if value and value not in ("none", "scoped", "direct", "host"):
        raise SandboxError(
            f"{NETWORK_OVERRIDE_ENV} must be one of none/scoped/direct/host: {value!r}"
        )
    return value or None
