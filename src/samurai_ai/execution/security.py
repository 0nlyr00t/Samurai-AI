"""Governed adapters for external security tools (threat-review waves 1-4).

Each adapter wraps a sandbox profile with a fixed argv template: the model
picks values from validated parameters, the adapter builds the command line
itself. Targets are scope-checked before the container spawns. Egress goes
through the scoped, token-authenticated proxy except on `direct`-class
profiles, where the adapter's scope check is the only gate (see
md/threat-review-security-tools.md 2.7). Tool stdout is parsed into a bounded
structured summary while the raw output stays in the artifact.

Secrets never enter argv: provider keys are passed as env names and the
value comes from the docker client environment (see
md/threat-review-security-tools.md 2.7).

See md/threat-review-security-tools.md for the decision record.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from samurai_ai.domain.execution import (
    NetworkAccess,
    ToolDefinition,
    ToolExecutionError,
    ToolKind,
    ToolScopeError,
    ToolValidationError,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.registry import ToolOutput
from samurai_ai.execution.sandbox import DockerSandboxRunner, get_profile

_NO_COMMA_OR_SPACE = re.compile(r"^[^\s,]+$")
_PORTS = re.compile(r"^\d{1,5}(?:-\d{1,5})?(?:,\d{1,5}(?:-\d{1,5})?)*$")
_SUMMARY_LIMIT = 100
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _wordlist_dir() -> Path:
    """`assets/wordlists`, also inside a PyInstaller one-file bundle."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        return Path(bundle) / "assets" / "wordlists"
    return _REPO_ROOT / "assets" / "wordlists"
_FUZZ_MATCH_CODES = "200-299,301,302,307,401,403,405,500"


#: parameter key the executor uses to carry vault-resolved env to the tool
_SECRET_ENV_KEY = "_secret_env"


class _ScopedSandboxTool:
    profile_id: str = ""
    executable: str = ""
    max_targets: int = 1

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        self._runner = runner
        self._notice: str | None = None

    @property
    def runner(self) -> DockerSandboxRunner:
        if self._runner is None:
            from samurai_ai.config import sandbox_allowlist_path, vault_key_path
            from samurai_ai.services.vault import load_master_key

            self._runner = DockerSandboxRunner(
                ProcessManager(allowed_executables=frozenset({"docker"})),
                sandbox_allowlist_path(),
                lambda: load_master_key(vault_key_path()),
            )
        return self._runner

    @property
    def definition(self) -> ToolDefinition:
        raise NotImplementedError

    def build_args(self, params: dict[str, Any]) -> list[str]:
        raise NotImplementedError

    def build_env(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Plain env (value in argv) and secret env (name in argv only)."""
        return {}, {}

    def build_stdin(self, params: dict[str, Any]) -> bytes | None:
        """Payload for tools that read their targets from stdin."""
        return None

    def summarize(self, stdout: str) -> list[str]:
        raise NotImplementedError

    def _validated_targets(
        self, params: dict[str, Any], scope: ScopeContract
    ) -> list[str]:
        raw_targets = params["targets"]
        targets = [str(target) for target in raw_targets]
        if not 1 <= len(targets) <= self.max_targets:
            raise ToolValidationError(
                f"{self.definition.id}: expected 1..{self.max_targets} targets, "
                f"got {len(targets)}"
            )
        for target in targets:
            if not _NO_COMMA_OR_SPACE.fullmatch(target):
                raise ToolValidationError(
                    f"{self.definition.id}: target must not contain spaces or "
                    f"commas: {target!r}"
                )
            decision = scope.evaluate(target)
            if not decision.allowed:
                raise ToolScopeError(
                    f"{self.definition.id}: target {target!r} is out of scope: "
                    f"{decision.detail}"
                )
        return targets

    def preflight(self, params: dict[str, Any]) -> None:
        profile = get_profile(self.profile_id)
        args = self.build_args(params)
        self.runner.validate_invocation(profile, self.executable, args)
        _, secret_env = self.build_env(params)
        if secret_env:
            self.runner.validate_secret_env(profile, secret_env)
        self._notice = asyncio.run(self.runner.preflight(profile))

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        profile = get_profile(self.profile_id)
        targets = self._validated_targets(params, scope)
        args = self.build_args({**params, "targets": targets})
        env, secret_env = self.build_env(params)
        # env the executor already resolved from the vault (declared names only)
        extra = params.get(_SECRET_ENV_KEY) or {}
        if isinstance(extra, dict):
            secret_env = {**secret_env, **{str(k): str(v) for k, v in extra.items()}}
        run_id = uuid4().hex[:12]
        outcome = self.runner.run(
            profile,
            run_id,
            self.executable,
            args,
            scope,
            env=env,
            secret_env=secret_env,
            stdin_data=self.build_stdin({**params, "targets": targets}),
        )
        result = outcome.result
        summary_lines = self.summarize(result.stdout)
        stdout = ""
        if summary_lines:
            stdout = "# summary\n" + "\n".join(summary_lines) + "\n\n# raw\n"
        stdout += result.stdout
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
            stdout=stdout,
            stderr=stderr,
            exit_code=result.exit_code if result.exit_code is not None else -1,
            timed_out=result.timed_out,
            secrets=(
                *((outcome.proxy_token,) if outcome.proxy_token else ()),
                *secret_env.values(),
            ),
            egress_events=outcome.egress_events,
        )


def _targets_schema(max_items: int) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "maxItems": max_items,
    }


def _explicit_port(target: str) -> bool:
    """Does this target name a port of its own? Then httpx must not add one."""
    try:
        return urlsplit(target).port is not None
    except ValueError:
        return False


class HttpxProbeTool(_ScopedSandboxTool):
    profile_id = "sandbox.httpx@v1"
    executable = "httpx"
    max_targets = 50

    _PROFILE_FLAGS = {
        "headers": ["-status-code", "-title"],
        "tech": ["-status-code", "-tech-detect"],
        "full": ["-status-code", "-title", "-tech-detect", "-web-server"],
    }

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="recon.httpx-probe",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Probe in-scope web targets with httpx in the scoped "
            "sandbox (status, title, tech fingerprint)",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.PROXIED,
            timeout_seconds=300,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    "targets": _targets_schema(self.max_targets),
                    "profile": {
                        "type": "string",
                        "enum": list(self._PROFILE_FLAGS),
                        "default": "headers",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "default": 30,
                    },
                },
                "required": ["targets"],
                "additionalProperties": False,
            },
            capabilities=("recon", "http-probe"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def build_args(self, params: dict[str, Any]) -> list[str]:
        targets = [str(target) for target in params["targets"]]
        timeout = int(params.get("timeout_seconds", 30))
        if not 1 <= timeout <= 60:
            raise ToolValidationError(
                f"{self.definition.id}: timeout_seconds must be 1..60, got {timeout}"
            )
        profile = str(params.get("profile", "headers"))
        flags = self._PROFILE_FLAGS.get(profile)
        if flags is None:
            raise ToolValidationError(
                f"{self.definition.id}: unknown profile {profile!r}"
            )
        argv = [
            "-json",
            "-silent",
            "-no-color",
            "-timeout",
            str(timeout),
            "-rate-limit",
            "50",
        ]
        # a URL that names its own port is authoritative; otherwise only the
        # web ports are worth probing, which is what -ports covers
        if not any(_explicit_port(target) for target in targets):
            argv += ["-ports", "80,443"]
        return [*argv, *flags, "-u", ",".join(targets)]

    def summarize(self, stdout: str) -> list[str]:
        lines: list[str] = []
        for raw_line in stdout.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            url = record.get("url") or record.get("input") or "?"
            status = record.get("status_code", record.get("status-code", "?"))
            title = str(record.get("title", ""))[:80]
            tech = record.get("tech") or record.get("technologies") or []
            tech_text = ",".join(str(item) for item in tech[:5])
            lines.append(f"{url}\tstatus={status}\ttitle={title}\ttech={tech_text}")
            if len(lines) >= _SUMMARY_LIMIT:
                break
        return lines


class NucleiScanTool(_ScopedSandboxTool):
    profile_id = "sandbox.nuclei@v1"
    executable = "nuclei"
    max_targets = 10

    _SEVERITIES = ("info", "low", "medium", "high", "critical")
    _TEMPLATE_TAGS = {
        "cves": "cve",
        "exposures": "exposure",
        "misconfig": "misconfig",
    }

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="scan.nuclei",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Run nuclei template scan against in-scope targets in "
            "the scoped sandbox (bundled templates, updates disabled)",
            risk_class=RiskClass.R3,
            network=NetworkAccess.PROXIED,
            timeout_seconds=600,
            output_limit_bytes=524288,
            parameters={
                "type": "object",
                "properties": {
                    "targets": _targets_schema(self.max_targets),
                    "severity": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(self._SEVERITIES)},
                        "default": list(self._SEVERITIES),
                    },
                    "template_set": {
                        "type": "string",
                        "enum": list(self._TEMPLATE_TAGS),
                        "default": "cves",
                    },
                },
                "required": ["targets"],
                "additionalProperties": False,
            },
            capabilities=("scan", "vulnerability-scan"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def build_args(self, params: dict[str, Any]) -> list[str]:
        targets = [str(target) for target in params["targets"]]
        severities = params.get("severity") or list(self._SEVERITIES)
        template_set = str(params.get("template_set", "cves"))
        tag = self._TEMPLATE_TAGS.get(template_set)
        if tag is None:
            raise ToolValidationError(
                f"{self.definition.id}: unknown template_set {template_set!r}"
            )
        return [
            "-jsonl",
            "-disable-update-check",
            "-stats-json",
            "-rate-limit",
            "50",
            "-c",
            "10",
            "-bulk-size",
            "5",
            "-timeout",
            "10",
            "-u",
            ",".join(targets),
            "-severity",
            ",".join(str(severity) for severity in severities),
            "-tags",
            tag,
        ]

    def summarize(self, stdout: str) -> list[str]:
        lines: list[str] = []
        for raw_line in stdout.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or "template" not in record:
                continue
            info = record.get("info") or {}
            severity = info.get("severity", "?")
            name = str(info.get("name", record.get("template", "?")))[:80]
            matched = str(record.get("matched-at", record.get("host", "?")))[:120]
            lines.append(f"[{severity}] {name}\t{matched}")
            if len(lines) >= _SUMMARY_LIMIT:
                break
        return lines


class SubfinderPassiveTool(_ScopedSandboxTool):
    """Passive subdomain enumeration; provider keys travel as env names only.

    The API-key channel is decided in md/threat-review-security-tools.md 2.7:
    a key never enters argv, only the declared variable name does, and the
    value comes from the vault through a ``{{secret:name}}`` parameter.
    """

    profile_id = "sandbox.subfinder@v1"
    executable = "subfinder"
    max_targets = 1

    # Sources that answer without a provider key (subfinder NoKey/OptionalKey).
    _KEYLESS = (
        "crtsh",
        "anubis",
        "waybackarchive",
        "commoncrawl",
        "sitedossier",
        "threatcrowd",
        "rapiddns",
        "hackertarget",
        "subdomaincenter",
    )
    # Sources that only answer with a key; subfinder reads "{SOURCE}_API_KEY".
    _KEYED = (
        "shodan",
        "virustotal",
        "securitytrails",
        "github",
        "censys",
        "urlscan",
        "chaos",
        "c99",
        "certspotter",
        "dnsdumpster",
        "quake",
        "intelx",
        "digitalyama",
        "fofa",
        "zoomeyeapi",
        "windvane",
    )
    _ALL_SOURCES = (*_KEYLESS, *_KEYED)

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="recon.subfinder-passive",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Passive subdomain enumeration for an in-scope domain "
            "through free sources; provider keys are vault secrets passed by "
            "env name only",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.PROXIED,
            timeout_seconds=300,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "minLength": 1},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(self._ALL_SOURCES),
                        },
                        "maxItems": len(self._ALL_SOURCES),
                    },
                    "keys": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "timeout_seconds": {"type": "integer", "default": 30},
                },
                "required": ["domain"],
                "additionalProperties": False,
            },
            capabilities=("recon", "subdomain-enumeration"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def _validated_targets(
        self, params: dict[str, Any], scope: ScopeContract
    ) -> list[str]:
        domain = str(params["domain"])
        if not _NO_COMMA_OR_SPACE.fullmatch(domain) or domain.startswith("-"):
            raise ToolValidationError(
                f"{self.definition.id}: domain must be a bare name without "
                f"spaces, commas or a leading dash: {domain!r}"
            )
        decision = scope.evaluate(domain)
        if not decision.allowed:
            raise ToolScopeError(
                f"{self.definition.id}: domain {domain!r} is out of scope: "
                f"{decision.detail}"
            )
        return [domain]

    def _validated_keys(self, params: dict[str, Any]) -> dict[str, str]:
        raw_keys = params.get("keys") or {}
        if not isinstance(raw_keys, dict):
            raise ToolValidationError(f"{self.definition.id}: keys must be an object")
        keys: dict[str, str] = {}
        for source, reference in raw_keys.items():
            source = str(source)
            if source in self._KEYLESS:
                raise ToolValidationError(
                    f"{self.definition.id}: source {source!r} needs no key"
                )
            if source not in self._KEYED:
                raise ToolValidationError(
                    f"{self.definition.id}: unknown keyed source {source!r}"
                )
            value = str(reference)
            if not value:
                raise ToolValidationError(
                    f"{self.definition.id}: key for {source!r} is empty"
                )
            if "\n" in value or "\r" in value:
                raise ToolValidationError(
                    f"{self.definition.id}: key for {source!r} must be a single line"
                )
            keys[source] = value
        return keys

    def _validated_sources(self, params: dict[str, Any]) -> list[str]:
        keys = self._validated_keys(params)
        requested = [str(source) for source in params.get("sources") or ()]
        unknown = [source for source in requested if source not in self._ALL_SOURCES]
        if unknown:
            raise ToolValidationError(
                f"{self.definition.id}: unknown sources {', '.join(unknown)}"
            )
        sources = list(dict.fromkeys([*(requested or self._KEYLESS), *keys]))
        missing = [
            source for source in sources if source in self._KEYED and source not in keys
        ]
        if missing:
            raise ToolValidationError(
                f"{self.definition.id}: sources need a key in 'keys': "
                f"{', '.join(missing)}"
            )
        return sources

    def build_args(self, params: dict[str, Any]) -> list[str]:
        targets = params.get("targets") or [params["domain"]]
        timeout = int(params.get("timeout_seconds", 30))
        if not 1 <= timeout <= 60:
            raise ToolValidationError(
                f"{self.definition.id}: timeout_seconds must be 1..60, got {timeout}"
            )
        sources = self._validated_sources(params)
        return [
            "-silent",
            "-json",
            "-nc",
            "-disable-update-check",
            "-t",
            "10",
            "-timeout",
            str(timeout),
            "-d",
            str(targets[0]),
            "-s",
            ",".join(sources),
        ]

    def build_env(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        keys = self._validated_keys(params)
        # HOME on the tmpfs: subfinder creates its provider config file and the
        # rootfs is read-only. Values here carry no secrets.
        env = {"HOME": "/tmp", "TMPDIR": "/tmp"}
        secret_env = {
            f"{source.upper()}_API_KEY": value for source, value in keys.items()
        }
        return env, secret_env

    def summarize(self, stdout: str) -> list[str]:
        lines: list[str] = []
        for raw_line in stdout.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            host = record.get("host") or record.get("input")
            if not host:
                continue
            source = str(record.get("source", "?"))[:24]
            lines.append(f"{host}\t[{source}]")
            if len(lines) >= _SUMMARY_LIMIT:
                break
        return lines


class DnsxResolveTool(_ScopedSandboxTool):
    """Resolve in-scope names through the pinned DoH resolver over SOCKS5.

    The resolver lives in the signed profile, so the model cannot redirect
    lookups. A resolver that answers nothing while the run succeeded is
    reported as a failure: otherwise "no names" and "dead resolver" would read
    the same. `-rcode noerror,nxdomain` makes a live resolver answer once per
    name, which is what tells the two apart.
    """

    profile_id = "sandbox.dnsx@v1"
    executable = "dnsx"
    max_targets = 50

    _TYPE_FLAGS = {"A": "-a", "AAAA": "-aaaa", "CNAME": "-cname"}

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="recon.dnsx-resolve",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Resolve in-scope hosts through the pinned DoH resolver "
            "(records per name, candidate assets for the ledger)",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.PROXIED,
            timeout_seconds=180,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    "hosts": _targets_schema(self.max_targets),
                    "types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(self._TYPE_FLAGS),
                        },
                        "minItems": 1,
                        "maxItems": len(self._TYPE_FLAGS),
                        "default": ["A"],
                    },
                },
                "required": ["hosts"],
                "additionalProperties": False,
            },
            capabilities=("recon", "dns-resolution"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def _validated_targets(
        self, params: dict[str, Any], scope: ScopeContract
    ) -> list[str]:
        hosts = [str(host) for host in params["hosts"]]
        if not 1 <= len(hosts) <= self.max_targets:
            raise ToolValidationError(
                f"{self.definition.id}: expected 1..{self.max_targets} hosts, "
                f"got {len(hosts)}"
            )
        for host in hosts:
            if not _NO_COMMA_OR_SPACE.fullmatch(host) or host.startswith("-"):
                raise ToolValidationError(
                    f"{self.definition.id}: host must be a bare name without "
                    f"spaces, commas or a leading dash: {host!r}"
                )
            decision = scope.evaluate(host)
            if not decision.allowed:
                raise ToolScopeError(
                    f"{self.definition.id}: host {host!r} is out of scope: "
                    f"{decision.detail}"
                )
        return hosts

    def _validated_types(self, params: dict[str, Any]) -> list[str]:
        types = [str(item) for item in params.get("types") or ["A"]]
        if not types:
            raise ToolValidationError(f"{self.definition.id}: types must not be empty")
        for item in types:
            if item not in self._TYPE_FLAGS:
                raise ToolValidationError(
                    f"{self.definition.id}: unknown record type {item!r}"
                )
        return list(dict.fromkeys(types))

    def _resolver(self) -> str:
        resolver = get_profile(self.profile_id).resolver_url
        if resolver is None:
            raise ToolValidationError(
                f"{self.definition.id}: sandbox profile {self.profile_id} pins no "
                "resolver"
            )
        return resolver

    def build_args(self, params: dict[str, Any]) -> list[str]:
        # dnsx 1.2.2 has no -timeout flag: the profile timeout governs the run.
        type_flags = [self._TYPE_FLAGS[item] for item in self._validated_types(params)]
        return [
            "-silent",
            "-json",
            "-omit-raw",
            "-nc",
            "-disable-update-check",
            "-rcode",
            "noerror,nxdomain",
            "-r",
            self._resolver(),
            *type_flags,
        ]

    def build_stdin(self, params: dict[str, Any]) -> bytes:
        # dnsx 1.2.2 ignores -l, so the host list goes through stdin.
        return "".join(f"{str(host)}\n" for host in params["targets"]).encode("utf-8")

    def summarize(self, stdout: str) -> list[str]:
        lines: list[str] = []
        with_records = False
        for raw_line in stdout.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            host = record.get("host") or record.get("input")
            if not host:
                continue
            status = str(record.get("status_code", "?"))
            found: list[str] = []
            for record_type in ("a", "aaaa", "cname"):
                values = record.get(record_type) or ()
                if isinstance(values, str):
                    values = [values]
                if values:
                    found.append(
                        f"{record_type.upper()}={','.join(str(value) for value in values[:5])}"
                    )
            if found:
                with_records = True
            lines.append(f"{host}\t{status}\t{' '.join(found)}".rstrip())
            if len(lines) >= _SUMMARY_LIMIT:
                break
        if lines and not with_records:
            lines.append("no answers")
        return lines

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        output = super().invoke(params, scope)
        # Zero parsed lines on a successful exit means the resolver never
        # answered — not that the names are dead.
        if output.exit_code == 0 and not output.stdout.startswith("# summary\n"):
            raise ToolExecutionError(
                f"{self.definition.id}: resolver {self._resolver()} answered nothing "
                f"(exit 0, no records, no NXDOMAIN)"
            )
        return output


class NmapScanTool(_ScopedSandboxTool):
    """Port scan on a direct-class profile: the adapter is the only gate.

    `-sT` keeps the sandbox at `--cap-drop ALL` (no raw sockets) and `-Pn`
    always, because ping discovery needs raw ICMP too. nmap's own `--proxies`
    covers only version detection and takes no credentials, so the proxy is not
    in the path at all — the egress journal has no entry for this class.
    """

    profile_id = "sandbox.nmap@v1"
    executable = "nmap"
    max_targets = 10

    _PROFILE_FLAGS = {"fast": ["-F"], "standard": ["--top-ports", "1000"]}

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="scan.nmap",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Scan in-scope hosts for open TCP ports (connect scan, "
            "no ping, rate capped)",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.DIRECT,
            timeout_seconds=600,
            output_limit_bytes=524288,
            parameters={
                "type": "object",
                "properties": {
                    "targets": _targets_schema(self.max_targets),
                    "profile": {
                        "type": "string",
                        "enum": list(self._PROFILE_FLAGS),
                    },
                    "ports": {"type": "string", "minLength": 1},
                    "service_detection": {"type": "boolean", "default": False},
                },
                "required": ["targets"],
                "additionalProperties": False,
            },
            capabilities=("scan", "port-scan"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def _validated_ports(self, params: dict[str, Any]) -> list[str] | None:
        ports = params.get("ports")
        profile = params.get("profile")
        if ports is not None and profile is not None:
            # a model names both when it means "these ports, quickly": the
            # explicit list is the more specific answer, so it wins
            profile = None
        if ports is None and profile is None:
            return None
        if ports is not None:
            value = str(ports)
            if not _PORTS.fullmatch(value):
                raise ToolValidationError(
                    f"{self.definition.id}: ports must look like 80,443,8000-8100: "
                    f"{value!r}"
                )
            for part in value.split(","):
                high = int(part.split("-")[-1])
                if high > 65535:
                    raise ToolValidationError(
                        f"{self.definition.id}: port {high} is above 65535"
                    )
            return ["-p", value]
        flags = self._PROFILE_FLAGS.get(str(profile))
        if flags is None:
            raise ToolValidationError(
                f"{self.definition.id}: unknown profile {profile!r}"
            )
        return list(flags)

    def build_args(self, params: dict[str, Any]) -> list[str]:
        port_flags = self._validated_ports(params)
        if port_flags is None:
            port_flags = list(self._PROFILE_FLAGS["fast"])
        args = [
            "-Pn",
            "-sT",
            "-n",
            "--max-rate",
            "300",
            *port_flags,
            "-oX",
            "-",
        ]
        if params.get("service_detection"):
            args += ["-sV", "--version-light"]
        args += [str(target) for target in params["targets"]]
        return args

    def summarize(self, stdout: str) -> list[str]:
        try:
            root = ElementTree.fromstring(stdout)
        except ElementTree.ParseError:
            return []
        lines: list[str] = []
        for host in root.iter("host"):
            address = host.find("address")
            if address is None:
                continue
            name = address.get("addr", "?")
            for port in host.iter("port"):
                state = port.find("state")
                if state is None or state.get("state") != "open":
                    continue
                service = port.find("service")
                service_text = ""
                if service is not None:
                    label = service.get("name", "")
                    version = " ".join(
                        part
                        for part in (
                            service.get("product", ""),
                            service.get("version", ""),
                        )
                        if part
                    )
                    service_text = f" {label}{(' ' + version) if version else ''}"
                lines.append(
                    f"{name}:{port.get('portid', '?')}/{port.get('protocol', 'tcp')}"
                    f" open{service_text}"
                )
                if len(lines) >= _SUMMARY_LIMIT:
                    return lines
        return lines or ["no open ports"]


class FfufFuzzTool(_ScopedSandboxTool):
    """Content discovery by fuzzing a URL path with a bundled wordlist.

    The wordlist rides on stdin (`-w /dev/stdin:FUZZ`) because the sandbox does
    not mount host paths; `assets/wordlists` pins the file by sha256 in a test.
    Noise is handled by the stock status matcher plus `-ac`, otherwise soft-404
    and SPA routers answer everything.
    """

    profile_id = "sandbox.ffuf@v1"
    executable = "ffuf"
    max_targets = 1

    _EXTENSIONS = (".php", ".bak", ".js", ".txt", ".zip")
    WORDLIST = "common.txt"

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="fuzz.ffuf",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Fuzz an in-scope URL path (FUZZ keyword) with a bundled "
            "wordlist through the scoped proxy",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.PROXIED,
            timeout_seconds=300,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                    "extensions": {
                        "type": "array",
                        # a closed enum made the model retry with its own
                        # guesses; any `.ext` is fine, ffuf takes them as text
                        "items": {
                            "type": "string",
                            "pattern": r"^\.?[a-z0-9]{1,8}$",
                        },
                        "maxItems": 8,
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            capabilities=("fuzz", "content-discovery"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def _validated_targets(
        self, params: dict[str, Any], scope: ScopeContract
    ) -> list[str]:
        url = str(params["url"])
        if "FUZZ" not in url:
            raise ToolValidationError(
                f"{self.definition.id}: url must contain the FUZZ keyword: {url!r}"
            )
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ToolValidationError(
                f"{self.definition.id}: url must be http(s) with a host: {url!r}"
            )
        decision = scope.evaluate(parts.hostname)
        if not decision.allowed:
            raise ToolScopeError(
                f"{self.definition.id}: host {parts.hostname!r} is out of scope: "
                f"{decision.detail}"
            )
        return [url]

    _EXTENSION = re.compile(r"\.[a-z0-9]{1,8}\Z")

    def _validated_extensions(self, params: dict[str, Any]) -> list[str]:
        # a model writes `html` as often as `.html`; the dot is not worth a retry
        extensions = [
            item if item.startswith(".") else f".{item}"
            for item in (str(value) for value in params.get("extensions") or ())
        ]
        for item in extensions:
            if not self._EXTENSION.match(item):
                raise ToolValidationError(
                    f"{self.definition.id}: extension must look like .php: {item!r}"
                )
        return list(dict.fromkeys(extensions))

    def build_args(self, params: dict[str, Any]) -> list[str]:
        targets = params.get("targets") or [params["url"]]
        args = [
            "-s",
            "-noninteractive",
            "-of",
            "json",
            "-o",
            "/dev/stdout",
            "-ac",
            "-mc",
            _FUZZ_MATCH_CODES,
            "-rate",
            "50",
            "-t",
            "20",
            "-w",
            "/dev/stdin:FUZZ",
            "-u",
            str(targets[0]),
        ]
        extensions = self._validated_extensions(params)
        if extensions:
            args += ["-e", ",".join(extensions)]
        return args

    def build_env(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        # ffuf writes a scraper config under $HOME and the rootfs is read-only.
        return {"HOME": "/tmp", "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/root/.cache"}, {}

    def build_stdin(self, params: dict[str, Any]) -> bytes:
        return (_wordlist_dir() / self.WORDLIST).read_bytes()

    def summarize(self, stdout: str) -> list[str]:
        try:
            document = json.loads(stdout[stdout.index("{"):])
        except (ValueError, json.JSONDecodeError):
            return []
        if not isinstance(document, dict):
            return []
        lines: list[str] = []
        for result in document.get("results") or ():
            if not isinstance(result, dict):
                continue
            url = str(result.get("url", "?"))[:200]
            status = result.get("status", "?")
            size = result.get("length", "?")
            words = result.get("words", "?")
            lines.append(f"{url}\tstatus={status}\tsize={size}\twords={words}")
            if len(lines) >= _SUMMARY_LIMIT:
                break
        return lines or ["no matches"]


__all__ = [
    "DnsxResolveTool",
    "FfufFuzzTool",
    "HttpxProbeTool",
    "NmapScanTool",
    "NucleiScanTool",
    "SubfinderPassiveTool",
]


def _research_payload(stdout: str) -> dict[str, Any] | None:
    """The adapter's JSON answer; the raw block below it is not parsed."""
    text = stdout.split("# screenshot", 1)[0]
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


_PROXY_BOOTSTRAP = """
import json, os, sys
from urllib.parse import urlsplit
from camoufox.sync_api import Camoufox

raw = os.environ.get("HTTP_PROXY", "")
opts = {"headless": True}
if raw:
    parts = urlsplit(raw)
    opts["proxy"] = {
        "server": "http://%s:%s" % (parts.hostname, parts.port),
        "username": parts.username,
        "password": parts.password,
    }
"""

_READ_SCRIPT = _PROXY_BOOTSTRAP + """
url, max_chars, want_shot = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1"
with Camoufox(**opts) as browser:
    page = browser.new_page()
    response = page.goto(url, timeout=45000, wait_until="domcontentloaded")
    status = response.status if response else 0
    links = []
    for anchor in page.query_selector_all("a")[:30]:
        href = anchor.get_attribute("href") or ""
        if href:
            links.append(href)
    shot = ""
    if want_shot:
        import base64
        shot = base64.b64encode(page.screenshot(type="png")).decode()
    print(json.dumps({
        "url": url,
        "status": status,
        "title": page.title()[:200],
        "text": page.inner_text("body")[:max_chars],
        "links": links,
        "screenshot": shot,
    }))
"""

_SEARCH_SCRIPT = _PROXY_BOOTSTRAP + """
query, count = sys.argv[1], int(sys.argv[2])
search_url = "https://html.duckduckgo.com/html/?q=" + query.replace(" ", "+")
results = []
with Camoufox(**opts) as browser:
    page = browser.new_page()
    response = page.goto(search_url, timeout=45000, wait_until="domcontentloaded")
    status = response.status if response else 0
    for anchor in page.query_selector_all("a.result__a")[:count]:
        results.append({"title": (anchor.inner_text() or "").strip()[:160],
                        "url": anchor.get_attribute("href") or ""})
print(json.dumps({"query": query, "search_url": search_url, "status": status,
                  "results": results}))
"""


class ResearchReadTool(_ScopedSandboxTool):
    """Render one URL in camoufox and bring back text, links and a citation.

    The browser executes foreign JavaScript, so it runs in the sandbox and its
    only way out is the scoped proxy. What it read is recorded as a traffic pair
    — a citation that cannot be re-opened is indistinguishable from a guess.
    """

    profile_id = "sandbox.camoufox@v1"
    executable = "python3"
    max_targets = 1

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="research.read",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Render an in-scope URL in an anti-detect browser and "
            "return its title, readable text and links",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.PROXIED,
            timeout_seconds=600,
            output_limit_bytes=524288,
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                    "max_chars": {"type": "integer", "default": 4000},
                    "screenshot": {"type": "boolean", "default": False},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            capabilities=("research", "browse"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def _validated_targets(
        self, params: dict[str, Any], scope: ScopeContract
    ) -> list[str]:
        url = str(params["url"])
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ToolValidationError(
                f"{self.definition.id}: url must be http(s) with a host: {url!r}"
            )
        decision = scope.evaluate(parts.hostname)
        if not decision.allowed:
            raise ToolScopeError(
                f"{self.definition.id}: host {parts.hostname!r} is out of scope: "
                f"{decision.detail}"
            )
        return [url]

    def build_args(self, params: dict[str, Any]) -> list[str]:
        targets = params.get("targets") or [params["url"]]
        max_chars = int(params.get("max_chars", 4000))
        if not 500 <= max_chars <= 20000:
            raise ToolValidationError(
                f"{self.definition.id}: max_chars must be 500..20000, got {max_chars}"
            )
        wants_shot = "1" if params.get("screenshot") else "0"
        return ["-c", _READ_SCRIPT, str(targets[0]), str(max_chars), wants_shot]

    def build_env(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        # The container runs as nobody (HOME=/nonexistent, read-only), so HOME
        # moves to the tmpfs; the browser build stays in the image cache via
        # XDG_CACHE_HOME, otherwise camoufox downloads 100+ MB per run.
        return {"HOME": "/tmp", "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/root/.cache"}, {}

    def summarize(self, stdout: str) -> list[str]:
        payload = _research_payload(stdout)
        if payload is None:
            return []
        lines = [
            f"url: {payload.get('url', '?')}",
            f"status: {payload.get('status', '?')}",
            f"title: {str(payload.get('title', ''))[:160]}",
        ]
        links = [str(link) for link in payload.get("links") or ()][:20]
        lines.append(f"links: {len(links)}")
        lines += [f"link: {link}" for link in links]
        for raw_line in str(payload.get("text", "")).splitlines():
            if raw_line.strip():
                lines.append(raw_line.strip()[:200])
            if len(lines) >= _SUMMARY_LIMIT:
                break
        return lines

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        output = super().invoke(params, scope)
        payload = _research_payload(output.stdout)
        if payload is None:
            return output
        screenshot = str(payload.get("screenshot") or "")
        if screenshot:
            output = ToolOutput(
                stdout=f"{output.stdout}\n\n# screenshot (base64 png)\n{screenshot}\n",
                stderr=output.stderr,
                exit_code=output.exit_code,
                timed_out=output.timed_out,
                secrets=output.secrets,
                egress_events=output.egress_events,
                traffic=(
                    {
                        "method": "GET",
                        "url": str(payload.get("url", "")),
                        "status": int(payload.get("status") or 0),
                        "response_body": str(payload.get("text", "")),
                    },
                ),
            )
            return output
        return ToolOutput(
            stdout=output.stdout,
            stderr=output.stderr,
            exit_code=output.exit_code,
            timed_out=output.timed_out,
            secrets=output.secrets,
            egress_events=output.egress_events,
            traffic=(
                {
                    "method": "GET",
                    "url": str(payload.get("url", "")),
                    "status": int(payload.get("status") or 0),
                    "response_body": str(payload.get("text", "")),
                },
            ),
        )


class ResearchSearchTool(_ScopedSandboxTool):
    """Ask DuckDuckGo through the same browser; no keys, no subscription."""

    profile_id = "sandbox.camoufox@v1"
    executable = "python3"
    max_targets = 1

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="research.search",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Web search through DuckDuckGo in the sandbox browser "
            "(result titles and links)",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.PROXIED,
            timeout_seconds=300,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 200,
                    },
                    "count": {"type": "integer", "default": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            capabilities=("research", "search"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def _validated_targets(
        self, params: dict[str, Any], scope: ScopeContract
    ) -> list[str]:
        query = str(params["query"]).strip()
        if not query:
            raise ToolValidationError(f"{self.definition.id}: query is empty")
        if "\x00" in query:
            raise ToolValidationError(f"{self.definition.id}: query contains NUL")
        return [query]

    def build_args(self, params: dict[str, Any]) -> list[str]:
        query = (params.get("targets") or [params["query"]])[0]
        count = int(params.get("count", 10))
        if not 1 <= count <= 20:
            raise ToolValidationError(
                f"{self.definition.id}: count must be 1..20, got {count}"
            )
        return ["-c", _SEARCH_SCRIPT, str(query), str(count)]

    def build_env(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        return {"HOME": "/tmp", "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/root/.cache"}, {}

    def summarize(self, stdout: str) -> list[str]:
        payload = _research_payload(stdout)
        if payload is None:
            return []
        lines = [f"query: {payload.get('query', '?')}"]
        for result in (payload.get("results") or ())[: self.definition.version and 20]:
            if not isinstance(result, dict):
                continue
            title = str(result.get("title", ""))[:120]
            link = str(result.get("url", ""))[:200]
            lines.append(f"{title}\t{link}")
            if len(lines) >= _SUMMARY_LIMIT:
                break
        return lines or ["no results"]

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        output = super().invoke(params, scope)
        payload = _research_payload(output.stdout)
        if payload is None:
            return output
        lines = "; ".join(
            str(result.get("url", ""))
            for result in (payload.get("results") or ())[:10]
            if isinstance(result, dict)
        )
        search_url = str(payload.get("search_url", ""))
        return ToolOutput(
            stdout=output.stdout,
            stderr=output.stderr,
            exit_code=output.exit_code,
            timed_out=output.timed_out,
            secrets=output.secrets,
            egress_events=output.egress_events,
            traffic=(
                {
                    "method": "GET",
                    "url": search_url,
                    "status": int(payload.get("status") or 0),
                    "response_body": lines,
                },
            ),
        )


__all__ = [
    "DnsxResolveTool",
    "FfufFuzzTool",
    "HttpxProbeTool",
    "NmapScanTool",
    "NucleiScanTool",
    "ResearchReadTool",
    "ResearchSearchTool",
    "SubfinderPassiveTool",
]
