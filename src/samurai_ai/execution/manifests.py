"""Rendering and execution for manifest-described tools.

The renderer is the security boundary: it turns validated parameters into argv
tokens and refuses anything that could look like a flag. Parsers are a closed
registry, so a manifest picks a kind but never describes one.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from samurai_ai.domain.execution import ToolDefinition, ToolScopeError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.tool_manifest import ManifestError, ParamSpec, ToolManifest
from samurai_ai.execution.registry import ToolOutput
from samurai_ai.execution.sandbox import DockerSandboxRunner

_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
_UNSAFE = re.compile(r"[\s\x00]")
_NO_COMMA_OR_SPACE = re.compile(r"^[^\s,]+$")


def _render_scalar(spec: ParamSpec, name: str, value: Any, *, allow_dash: bool) -> str:
    if spec.type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ManifestError(f"parameter {name!r} must be an integer")
        if spec.minimum is not None and value < spec.minimum:
            raise ManifestError(f"parameter {name!r} is below {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise ManifestError(f"parameter {name!r} is above {spec.maximum}")
        text = str(value)
    elif spec.type == "boolean":
        if not isinstance(value, bool):
            raise ManifestError(f"parameter {name!r} must be a boolean")
        return "true" if value else "false"
    else:
        text = str(value)
        if spec.enum and text not in spec.enum:
            raise ManifestError(
                f"parameter {name!r} must be one of {', '.join(spec.enum)}"
            )
        if spec.pattern and re.fullmatch(spec.pattern, text) is None:
            raise ManifestError(f"parameter {name!r} does not match its pattern")
        if spec.max_length and len(text) > spec.max_length:
            raise ManifestError(f"parameter {name!r} is longer than {spec.max_length}")
    if not text:
        raise ManifestError(f"parameter {name!r} is empty")
    if _UNSAFE.search(text):
        raise ManifestError(
            f"parameter {name!r} must not contain spaces or NUL: {text!r}"
        )
    if text.startswith("-") and not allow_dash:
        # A bare value may not be mistaken for a flag by the tool's parser;
        # enumerated values are reviewed data, so they may be flags.
        raise ManifestError(f"parameter {name!r} must not start with '-': {text!r}")
    return text


def render_value(manifest: ToolManifest, name: str, value: Any) -> str:
    spec = manifest.params[name]
    if spec.type != "array":
        return _render_scalar(spec, name, value, allow_dash=bool(spec.enum))
    if not isinstance(value, (list, tuple)):
        raise ManifestError(f"parameter {name!r} must be an array")
    if spec.max_items and len(value) > spec.max_items:
        raise ManifestError(f"parameter {name!r} allows at most {spec.max_items} items")
    if not value:
        raise ManifestError(f"parameter {name!r} must not be empty")
    if spec.items_enum:
        allowed = ", ".join(spec.items_enum)
        for item in value:
            if str(item) not in spec.items_enum:
                raise ManifestError(f"parameter {name!r} items must be one of {allowed}")
    rendered = [
        _render_scalar(spec, name, item, allow_dash=True)
        for item in value
    ]
    return spec.separator.join(rendered)


def render_argv(manifest: ToolManifest, params: dict[str, Any]) -> list[str]:
    """Substitute placeholders; literal tokens come from the (reviewed) manifest."""
    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        value = params.get(name, manifest.params[name].default)
        return render_value(manifest, name, value)

    rendered: list[str] = []
    for token in manifest.argv:
        names = _PLACEHOLDER.findall(token)
        dropped = any(
            name in manifest.params
            and manifest.params[name].optional
            and params.get(name) is None
            for name in names
        )
        if dropped:
            # the flag itself carries nothing: take it along, or `--api-token`
            # would dangle in front of the next value
            if rendered and rendered[-1].startswith("-") and not _PLACEHOLDER.search(
                rendered[-1]
            ):
                rendered.pop()
            continue
        rendered.append(_PLACEHOLDER.sub(substitute, token))
    return rendered


def render_stdin(manifest: ToolManifest, params: dict[str, Any]) -> bytes | None:
    if manifest.stdin_from is None:
        return None
    value = params.get(manifest.stdin_from)
    if value is None:
        return None
    items = value if isinstance(value, (list, tuple)) else [value]
    return "".join(f"{item}\n" for item in items).encode("utf-8")


def _jsonl(stdout: str, limit: int) -> list[str]:
    lines: list[str] = []
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            pairs = (f"{key}={value}" for key, value in record.items() if key != "_raw")
            lines.append("\t".join(pairs))
        if len(lines) >= limit:
            break
    return lines


def _xml_ports(stdout: str, limit: int) -> list[str]:
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
            label = service.get("name", "") if service is not None else ""
            where = f"{name}:{port.get('portid', '?')}/{port.get('protocol', 'tcp')}"
            lines.append(f"{where} open {label}".rstrip())
            if len(lines) >= limit:
                return lines
    return lines


def _text_lines(stdout: str, limit: int) -> list[str]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return lines[:limit]


PARSERS = {"jsonl": _jsonl, "xml-ports": _xml_ports, "text-lines": _text_lines}


class ManifestTool:
    """A tool built from a manifest, holding the adapter contract."""

    def __init__(self, manifest: ToolManifest, runner: DockerSandboxRunner | None = None) -> None:
        self.manifest = manifest
        self._runner = runner
        self._notice: str | None = None
        self._definition = manifest.definition()

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def runner(self) -> DockerSandboxRunner:
        if self._runner is None:
            from samurai_ai.config import sandbox_allowlist_path, vault_key_path
            from samurai_ai.execution.process import ProcessManager
            from samurai_ai.services.vault import load_master_key

            self._runner = DockerSandboxRunner(
                ProcessManager(allowed_executables=frozenset({"docker"})),
                sandbox_allowlist_path(),
                lambda: load_master_key(vault_key_path()),
            )
        return self._runner

    def _validated_targets(
        self, params: dict[str, Any], scope: ScopeContract
    ) -> list[str]:
        targets = self.manifest.targets
        if targets is None:
            return []
        raw = params.get(targets.param)
        values = (
            [str(item) for item in raw]
            if isinstance(raw, (list, tuple))
            else ([str(raw)] if raw is not None else [])
        )
        if not 1 <= len(values) <= targets.max:
            raise ManifestError(
                f"{self.manifest.id}: expected 1..{targets.max} targets, got {len(values)}"
            )
        for value in values:
            candidate = value
            if targets.kind == "url-host":
                parts = urlsplit(value)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    raise ManifestError(f"{self.manifest.id}: not an http(s) URL: {value!r}")
                candidate = parts.hostname
            elif not _NO_COMMA_OR_SPACE.fullmatch(value) or value.startswith("-"):
                raise ManifestError(
                    f"{self.manifest.id}: target must be a bare host: {value!r}"
                )
            decision = scope.evaluate(candidate)
            if not decision.allowed:
                raise ToolScopeError(
                    f"{self.manifest.id}: target {candidate!r} is out of scope: "
                    f"{decision.detail}"
                )
        return values

    def preflight(self, params: dict[str, Any]) -> None:
        # Validate the render without spawning: a bad value must fail before
        # the operator is asked for an approval.
        render_argv(self.manifest, params)
        profile = self.manifest.profile()
        self.runner.validate_invocation(profile, self.manifest.executable, ["-h"])
        self._notice = self.runner.verify_allowlist_signature()

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        targets = self._validated_targets(params, scope)
        resolved = dict(params)
        target_param = self.manifest.targets
        if targets and target_param is not None:
            # a scalar targets parameter keeps its shape
            resolved[target_param.param] = (
                targets[0]
                if self.manifest.params[target_param.param].type == "string"
                else targets
            )
        argv = render_argv(self.manifest, resolved)
        profile = self.manifest.profile()
        run_id = uuid4().hex[:12]
        outcome = self.runner.run(
            profile,
            run_id,
            self.manifest.executable,
            argv,
            scope if profile.network_mode == "scoped" else None,
            env=dict(self.manifest.env) or None,
            stdin_data=render_stdin(self.manifest, resolved),
        )
        result = outcome.result
        lines = PARSERS[self.manifest.parser](result.stdout, self.manifest.summary_limit)
        stdout = ""
        if lines:
            stdout = "# summary\n" + "\n".join(lines) + "\n\n# raw\n"
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
            secrets=(outcome.proxy_token,) if outcome.proxy_token else (),
            egress_events=outcome.egress_events,
        )


__all__ = ["ManifestTool", "PARSERS", "render_argv", "render_stdin", "render_value"]
