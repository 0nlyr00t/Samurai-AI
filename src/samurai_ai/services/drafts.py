"""Drafting a new tool from its documentation: capture, redact, propose, confirm.

The model never gets an install shell. A page of vendor documentation is fetched
(local file or http(s), bounded), secrets are redacted, and the model is asked
for one JSON manifest/recipe candidate. Nothing is installed or registered here:
the result is an *untrusted draft* on an explicit output path, with a sidecar
that records where the text came from, its sha256 and what the model may have
invented. Only an operator reading the file and passing ``--trusted`` to
``samurai tool install`` turns it into a tool.

Confirmation is the anti-invention rule: a digest, an executable, a package or a
healthcheck flag that is not literally in the documentation keeps the draft
``incomplete`` with an explicit reason, and an incomplete draft is refused by
install. Offline is judged before a socket is touched.

Interactive tools (C2 sessions, PTY) are out of scope by construction: a draft
describes one non-interactive argv run, the same shape the catalog already
executes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from samurai_ai.agents.parser import _extract_json_object
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.tool_manifest import ToolManifest
from samurai_ai.execution.http import HttpEgressBroker
from samurai_ai.services.models import ModelService

#: documentation is read into memory, so the ceiling is a security boundary
MAX_DOC_BYTES = 262_144
FETCH_TIMEOUT = 10.0
_STATUS_READY = "ready"
_STATUS_INCOMPLETE = "incomplete"
_MANAGERS = ("apt", "brew", "project-local")
_DIGEST = re.compile(r"@sha256:([0-9a-f]{64})")
#: `executable` must be a bare command name, not a path the model guessed
_COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

_PROMPT = """\
You turn one page of tool documentation into a single JSON manifest candidate.

Rules:
- Answer with JSON only: no prose, no markdown fences.
- Copy the container image digest, the package name, the executable name and the
  healthcheck argv verbatim from the text. Never invent a digest or a package.
- The tool is non-interactive: one argv run, no PTY, no C2 session.
- `image` must be `repo@sha256:<64 hex>` with the digest found in the text.
- `argv` lists tokens after the executable; a value from `params` is written as
  `{param}`. Tokens are literal, never shell.
- `provision.healthcheck` is a read-only flag list without the executable
  (for example ["--version"]).
- Give every `argv` parameter a `required: true` or a `default`; an unused
  parameter is rejected.
- `network`: `scoped` needs `proxy_flag` or `proxy_via_env`; use `direct` only
  when the doc says the tool does its own HTTP; `none` means no network.
- `risk_class` is `r0`/`r1` for scanners, `r2` for anything that writes,
  `r3`+ only for tools that change the target and are not read-only.

Shape:
{"id": "scan.example", "title": "...", "image": "repo@sha256:...",
 "executable": "example", "risk_class": "r1", "network": "scoped",
 "read_only": true, "proxy_flag": "-proxy",
 "execution": {"native": true, "project_local": false, "container": true},
 "provision": {"packages": {"linux": {"manager": "apt", "package": "example"}},
               "healthcheck": ["--version"]},
 "params": {"target": {"type": "string", "required": true}},
 "targets": {"param": "target", "kind": "host", "max": 1},
 "argv": ["-t", "{target}"], "parser": "text-lines"}

Documentation:
<<<DOC
"""

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (
        # `Authorization: Basic …` / `Authorization: token …` (bearer covered)
        re.compile(r"(?i)\b(authorization\s*:\s*)(?!bearer\b)[^\r\n\"']{4,}"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|api[_-]?token|access[_-]?token|token|secret|password|passwd)"
            r"(\s*[:=]\s*)[\"']?([^\s\"'&#,]{4,})"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?<![\w-])sk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"(?<![\w-])gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"(?<![\w-])AKIA[0-9A-Z]{16}(?![\w-])"), "[REDACTED]"),
    # userinfo in a URL: `https://user:pass@host`
    (re.compile(r"(?i)\b(https?://)([^\s/@]+):([^\s/@]+)@"), r"\1[REDACTED]@"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
)


class DraftError(ValueError):
    """The draft could not be produced; nothing was written."""


def redact(text: str) -> tuple[str, int]:
    """Strip credentials out of documentation or out of a candidate's values.

    Returns the text and how many substitutions happened, so the operator knows
    something was hidden rather than missing.
    """
    count = 0
    for pattern, replacement in _SECRET_PATTERNS:
        text, hits = pattern.subn(replacement, text)
        count += hits
    return text, count


@dataclass(frozen=True)
class CapturedDoc:
    kind: str  #: "file" or "url"
    ref: str
    text: str
    truncated: bool


class DocReader:
    """Read documentation, bounded, from a local file or an http(s) URL."""

    def __init__(
        self,
        fetcher: Callable[[str, float, int], tuple[bytes, bool]] | None = None,
    ) -> None:
        self._fetcher = fetcher

    def read(self, source: str, *, offline: bool) -> CapturedDoc:
        parts = urlsplit(source)
        if parts.scheme in ("http", "https"):
            return self._read_url(source, parts, offline=offline)
        if parts.scheme:
            raise DraftError(
                f"unsupported source scheme {parts.scheme!r}: use a file path or http(s)"
            )
        return self._read_file(source)

    def _read_url(self, source: str, parts: Any, *, offline: bool) -> CapturedDoc:
        if offline:
            # judged before any resolution: an offline box must not wait on DNS
            raise DraftError(
                f"offline: refusing to fetch {source}; draft from a local file instead"
            )
        host = parts.hostname or ""
        if not host:
            raise DraftError(f"URL has no host: {source!r}")
        fetch = self._fetcher or self._broker_fetch(host)
        try:
            body, truncated = fetch(source, FETCH_TIMEOUT, MAX_DOC_BYTES)
        except DraftError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors are all refusals
            raise DraftError(f"cannot fetch {source}: {exc}") from exc
        if not body.strip():
            raise DraftError(f"{source} answered an empty body")
        return CapturedDoc(
            kind="url",
            ref=source,
            text=body.decode("utf-8", errors="replace"),
            truncated=truncated,
        )

    @staticmethod
    def _broker_fetch(host: str) -> Callable[[str, float, int], tuple[bytes, bool]]:
        """The existing broker, scoped to exactly the host that was asked for."""

        def fetch(url: str, timeout: float, max_bytes: int) -> tuple[bytes, bool]:
            broker = HttpEgressBroker(ScopeContract(includes=(host,)))
            response = broker.fetch(url, timeout=timeout, max_bytes=max_bytes)
            if response.status >= 400:
                raise DraftError(f"HTTP {response.status} for {url}")
            return response.body, response.truncated

        return fetch

    @staticmethod
    def _read_file(source: str) -> CapturedDoc:
        path = Path(source).expanduser()
        if not path.exists():
            raise DraftError(f"no such documentation file: {path}")
        if path.is_dir():
            raise DraftError(f"{path} is a directory; point at one file")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise DraftError(f"cannot read {path}: {exc}") from exc
        truncated = len(raw) > MAX_DOC_BYTES
        return CapturedDoc(
            kind="file",
            ref=str(path),
            text=raw[:MAX_DOC_BYTES].decode("utf-8", errors="replace"),
            truncated=truncated,
        )


class ModelCandidate:
    """The production extractor: one ModelService call, JSON in, JSON out.

    It is a plain callable so tests can pass a fixed string instead of a model.
    """

    def __init__(
        self,
        models: ModelService,
        operation_id: str,
        model_id: str | None = None,
        *,
        fallbacks: tuple[str, ...] = (),
        approval_id: str | None = None,
    ) -> None:
        self.models = models
        self.operation_id = operation_id
        self.model_id = model_id
        self.fallbacks = fallbacks
        self.approval_id = approval_id

    def __call__(self, documentation: str) -> str:
        model = self.model_id or self._fallback_model()
        if model is None:
            raise DraftError(
                "no model: pass --model or set a default with 'samurai model default'"
            )
        response = self.models.complete(
            self.operation_id,
            model,
            _PROMPT + documentation + "\nDOC\n",
            max_tokens=2000,
            fallbacks=self.fallbacks,
            approval_id=self.approval_id,
        )
        return response.text

    def _fallback_model(self) -> str | None:
        """The configured default, else the first known model: never a guess."""
        configured = self.models.default_model()
        if configured:
            return configured
        available = self.models.list_models()
        return available[0].id if available else None


class DraftMeta(BaseModel):
    """What the draft is, where it came from, and whether it may be installed."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    status: str
    source_kind: str
    source_ref: str
    document_sha256: str
    candidate_sha256: str
    redactions: int = 0
    truncated: bool = False
    model_id: str = ""
    missing: tuple[str, ...] = ()
    created_at: datetime

    @property
    def ready(self) -> bool:
        return self.status == _STATUS_READY


class Draft(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    output: Path
    sidecar: Path
    source_kind: str
    source_ref: str
    document_sha256: str
    candidate_sha256: str
    redactions: int
    truncated: bool
    missing: tuple[str, ...]
    candidate: dict[str, Any]

    @property
    def ready(self) -> bool:
        return self.status == _STATUS_READY


def sidecar_path(output: Path) -> Path:
    """`scan.foo.json` → `scan.foo.json.draft.json`; the manifest stays legal."""
    return output.with_name(output.name + ".draft.json")


def read_meta(path: Path) -> DraftMeta | None:
    """The sidecar next to a draft file, or None for a hand-written manifest."""
    sidecar = sidecar_path(path)
    if not sidecar.is_file():
        return None
    try:
        return DraftMeta.model_validate_json(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        return None


_MISSING = object()
#: a healthcheck flag every tool documents, so it cannot confirm anything
_GENERIC_FLAGS = frozenset({"--version", "-V", "--help", "-h"})


def _field(payload: dict[str, Any], dotted: str) -> Any:
    node: Any = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _mentions(document: str, value: str) -> bool:
    return bool(value) and value.lower() in document.lower()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def assess(document: str, candidate: dict[str, Any], *, templates: Path | None) -> list[str]:
    """Everything that keeps a candidate from being installed, in one list.

    Two kinds of reason share the list on purpose: a field the model left out and
    a field it invented read the same to the operator, and both mean "not yet".
    """
    problems: list[str] = []
    for dotted in (
        "id",
        "title",
        "image",
        "executable",
        "risk_class",
        "network",
        "read_only",
        "execution",
        "provision.packages",
        "argv",
    ):
        if _field(candidate, dotted) in (_MISSING, None):
            problems.append(f"missing required field: {dotted}")

    image = _field(candidate, "image")
    if isinstance(image, str):
        pin = _DIGEST.search(image)
        if pin is None:
            problems.append("image is not pinned by digest (repo@sha256:…)")
        elif not _mentions(document, pin.group(1)):
            problems.append(f"image digest {pin.group(1)[:12]}… is not in the documentation")

    executable = _field(candidate, "executable")
    if isinstance(executable, str):
        if not _COMMAND.fullmatch(executable):
            problems.append(f"executable is not a bare command name: {executable!r}")
        elif not _mentions(document, executable):
            problems.append(f"executable {executable!r} is not in the documentation")

    packages = _field(candidate, "provision.packages")
    package_count = 0
    if isinstance(packages, dict):
        for platform, spec in packages.items():
            if not isinstance(spec, dict):
                problems.append(f"provision.packages.{platform} is not an object")
                continue
            package_count += 1
            manager = spec.get("manager")
            name = spec.get("package")
            if manager not in _MANAGERS:
                problems.append(
                    f"provision.packages.{platform}: manager must be one of {', '.join(_MANAGERS)}"
                )
            if not isinstance(name, str) or not name:
                problems.append(f"provision.packages.{platform}: no package named")
                continue
            if manager == "project-local":
                # a local script is confirmed by the shipped file, not by prose
                if templates is None or not (templates / name).is_file():
                    problems.append(f"project-local template {name!r} is not shipped")
                continue
            if not _mentions(document, name):
                problems.append(f"package {name!r} is not in the documentation")
            if manager == "apt" or manager == "brew":
                if spec.get("reproducible"):
                    # a rolling repository cannot be confirmed from prose
                    problems.append(
                        f"provision.packages.{platform}: apt/brew repositories roll, "
                        "so reproducible must stay false"
                    )

    execution = _field(candidate, "execution")
    if isinstance(execution, dict):
        if not any(execution.get(key) for key in ("native", "project_local", "container")):
            problems.append(
                "execution declares no path: native, project_local or container is needed"
            )

    healthcheck = _field(candidate, "provision.healthcheck")
    if package_count:
        if healthcheck is _MISSING:
            problems.append("missing required field: provision.healthcheck")
        elif not _strings(healthcheck):
            problems.append("provision.healthcheck is empty: readiness cannot be proven")
        elif isinstance(executable, str) and executable in _strings(healthcheck):
            problems.append(
                "provision.healthcheck must be the flags only, without the executable"
            )
        else:
            for token in _strings(healthcheck):
                if token in _GENERIC_FLAGS:
                    continue
                if not _mentions(document, token):
                    problems.append(f"healthcheck {token!r} is not in the documentation")

    argv = _field(candidate, "argv")
    if isinstance(argv, list) and not argv:
        problems.append("argv is empty")
    return problems


class DraftService:
    """Produce a draft file plus its sidecar. Installs and registers nothing."""

    def __init__(
        self,
        *,
        reader: DocReader | None = None,
        extractor: Callable[[str], str] | None = None,
        templates: Path | None = None,
        model_id: str = "",
    ) -> None:
        self.reader = reader or DocReader()
        self.extractor = extractor
        self.templates = templates
        self.model_id = model_id

    def draft(
        self, source: str, output: Path, *, offline: bool = False
    ) -> Draft:
        if self.extractor is None:
            raise DraftError("no candidate extractor configured")
        captured = self.reader.read(source, offline=offline)
        redacted_doc, doc_hits = redact(captured.text)
        raw = self.extractor(redacted_doc)
        try:
            candidate = _extract_json_object(raw)
        except Exception as exc:  # noqa: BLE001 - AgentOutputError carries the text
            raise DraftError(f"model answer is not one JSON object: {exc}") from exc
        redacted_candidate, value_hits = redact(json.dumps(candidate, indent=2, ensure_ascii=False))
        payload: dict[str, Any] = json.loads(redacted_candidate)
        payload["trusted"] = False
        problems = assess(redacted_doc, payload, templates=self.templates)
        if not problems:
            try:
                ToolManifest.model_validate(payload)
            except ValidationError as exc:
                problems.extend(
                    f"invalid manifest: {error['loc']}: {error['msg']}"
                    if error.get("loc")
                    else f"invalid manifest: {error['msg']}"
                    for error in exc.errors()
                )
        return self._write(
            captured,
            payload,
            output,
            problems=tuple(problems),
            redactions=doc_hits + value_hits,
            # the digest covers exactly the text the model was shown
            document_sha256=hashlib.sha256(redacted_doc.encode("utf-8")).hexdigest(),
        )

    def _write(
        self,
        captured: CapturedDoc,
        payload: dict[str, Any],
        output: Path,
        *,
        problems: tuple[str, ...],
        redactions: int,
        document_sha256: str,
    ) -> Draft:
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        meta = DraftMeta(
            status=_STATUS_INCOMPLETE if problems else _STATUS_READY,
            source_kind=captured.kind,
            source_ref=captured.ref,
            document_sha256=document_sha256,
            candidate_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            redactions=redactions,
            truncated=captured.truncated,
            model_id=self.model_id,
            missing=problems,
            created_at=datetime.now(UTC),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        sidecar = sidecar_path(output)
        sidecar.write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return Draft(
            status=meta.status,
            output=output,
            sidecar=sidecar,
            source_kind=meta.source_kind,
            source_ref=meta.source_ref,
            document_sha256=meta.document_sha256,
            candidate_sha256=meta.candidate_sha256,
            redactions=meta.redactions,
            truncated=meta.truncated,
            missing=problems,
            candidate=payload,
        )


__all__ = [
    "CapturedDoc",
    "DocReader",
    "Draft",
    "DraftError",
    "DraftMeta",
    "DraftService",
    "MAX_DOC_BYTES",
    "ModelCandidate",
    "assess",
    "read_meta",
    "redact",
    "sidecar_path",
]
