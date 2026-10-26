"""Governed adapters for external security tools (threat-review wave 1).

Each adapter wraps a scoped-sandbox profile with a fixed argv template:
the model picks values from validated parameters, the adapter builds the
command line itself. Targets are scope-checked before the container spawns;
network egress goes only through the scoped, token-authenticated egress
proxy; tool stdout is parsed into a bounded structured summary while the
raw output stays in the artifact.

See md/threat-review-security-tools.md for the decision record.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from uuid import uuid4

from samurai_ai.domain.execution import (
    ToolDefinition,
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
_SUMMARY_LIMIT = 100


class _ScopedSandboxTool:
    profile_id: str = ""
    executable: str = ""
    max_targets: int = 1

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        self._runner = runner

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
        asyncio.run(self.runner.preflight(profile))

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        profile = get_profile(self.profile_id)
        targets = self._validated_targets(params, scope)
        args = self.build_args({**params, "targets": targets})
        run_id = uuid4().hex[:12]
        outcome = self.runner.run(profile, run_id, self.executable, args, scope)
        result = outcome.result
        summary_lines = self.summarize(result.stdout)
        stdout = ""
        if summary_lines:
            stdout = "# summary\n" + "\n".join(summary_lines) + "\n\n# raw\n"
        stdout += result.stdout
        stderr = result.stderr
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
            secrets=(outcome.proxy_token,) if outcome.proxy_token else (),
            egress_events=outcome.egress_events,
        )


def _targets_schema(max_items: int) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "maxItems": max_items,
    }


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
            network_access=False,
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
        return [
            "-json",
            "-silent",
            "-no-color",
            "-timeout",
            str(timeout),
            "-rate-limit",
            "50",
            "-ports",
            "80,443",
            *flags,
            "-u",
            ",".join(targets),
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
            network_access=False,
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


__all__ = ["HttpxProbeTool", "NucleiScanTool"]
