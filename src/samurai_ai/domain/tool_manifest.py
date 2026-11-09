"""Declarative tool manifests: an attacking tool as data, not as an adapter.

A manifest names a pinned image, the executable, a typed parameter schema and an
argv template built from placeholders. Values are validated before rendering
(no spaces, no NUL, no leading dash), so a parameter cannot smuggle a flag and a
flag cannot hide in a value. The sandbox profile is generated from the manifest,
which keeps one file per tool instead of two.

Parsers are a closed registry in code — the model and the manifest can only pick
a kind, never describe one.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from samurai_ai.domain.execution import (
    NetworkAccess,
    ToolDefinition,
    ToolKind,
    ToolValidationError,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.sandbox import SandboxProfile

PARSERS = ("jsonl", "xml-ports", "text-lines")
MAX_PARAMS = 12
MAX_ARGV = 40
_PARAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
_TOOL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+\Z")
#: a tag may sit before the digest: `python:3.12-slim@sha256:…`
_PINNED_IMAGE = re.compile(
    r"[a-zA-Z0-9][a-zA-Z0-9._/-]*(?::[a-zA-Z0-9._-]+)?@sha256:[0-9a-f]{64}\Z"
)
_UNSAFE_VALUE = re.compile(r"[\s\x00]")


class ManifestError(ToolValidationError):
    pass


class ParamSpec(BaseModel):
    """One typed parameter. Everything a value may be is declared here."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "boolean", "array"]
    description: str = ""
    required: bool = False
    #: a value that may legitimately be absent (a stored key that is not set):
    #: the whole argv entry carrying it is dropped instead of rendering empty
    optional: bool = False
    enum: tuple[str, ...] = ()
    pattern: str | None = None
    max_length: int | None = Field(default=None, ge=1, le=512)
    minimum: int | None = None
    maximum: int | None = None
    items_enum: tuple[str, ...] = ()
    max_items: int | None = Field(default=None, ge=1, le=50)
    separator: str = ","
    default: Any = None

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ManifestError(f"parameter pattern is not a regex: {exc}") from exc
        return value

    @field_validator("separator")
    @classmethod
    def validate_separator(cls, value: str) -> str:
        if not value or _UNSAFE_VALUE.search(value):
            raise ManifestError(f"parameter separator must be one bare character: {value!r}")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> ParamSpec:
        if self.enum and self.type not in ("string", "array"):
            raise ManifestError("enum is only meaningful for string or array parameters")
        if self.items_enum and self.type != "array":
            raise ManifestError("items_enum needs an array parameter")
        if self.type == "array" and self.separator in "{}":
            raise ManifestError("array separator cannot be a brace")
        return self


class PackageSpec(BaseModel):
    """How this tool is installed on one platform family.

    Only these keys exist, so the model can never name a package: the recipe
    does. `sudo` is a fact of the recipe, decided by review, not by the caller.
    """

    model_config = ConfigDict(extra="forbid")

    manager: Literal["apt", "brew", "project-local"]
    package: str = Field(min_length=1, max_length=120)
    #: argv after the manager, for recipes whose package is not the whole story
    extra: tuple[str, ...] = ()
    sudo: bool = False
    #: apt/brew repositories roll: the exact version is recorded, not promised
    reproducible: bool = False


class ManifestProvision(BaseModel):
    """Everything the provisioner needs, and nothing a shell would decide."""

    model_config = ConfigDict(extra="forbid")

    packages: dict[str, PackageSpec] = Field(default_factory=dict)
    #: read-only probes run after install, before the tool is declared ready
    healthcheck: tuple[str, ...] = ()
    version_flag: str = "--version"
    #: what the tool needs from the machine, checked before a plan is built
    requires_docker: bool = False
    requires_gpu: bool = False
    requires_sudo: bool = False
    min_free_mb: int = Field(default=0, ge=0)
    #: human-readable limits shown in the dry-run ("hashcat needs a GPU")
    limitations: tuple[str, ...] = ()
    #: ids this tool must not be installed next to
    conflicts: tuple[str, ...] = ()


class ManifestExecution(BaseModel):
    """Where the tool may run. Native is preferred where hardware matters."""

    model_config = ConfigDict(extra="forbid")

    #: argv without the executable: the same template for native and container
    native: bool = False
    project_local: bool = False
    container: bool = True
    relay_supported: bool = False
    #: working directory, relative to the operation's, when one is needed
    workdir: str = ""


class ManifestTargets(BaseModel):
    """Which parameter carries targets, and what may appear in it."""

    model_config = ConfigDict(extra="forbid")

    param: str
    kind: Literal["host", "domain", "url-host"] = "host"
    max: int = Field(default=10, ge=1, le=50)


class ManifestLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu_limit: str = "1.0"
    memory_mb: int = Field(default=256, ge=16, le=4096)
    pids_limit: int = Field(default=128, ge=16, le=1024)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_output_bytes: int = Field(default=131072, ge=1024, le=1048576)


class ToolManifest(BaseModel):
    """One tool as data. Validation is fail-closed and checks the whole shape."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = Field(default=1, ge=1)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=400)
    image: str
    executable: str = Field(min_length=1, max_length=64)
    risk_class: str = "r2"
    network: Literal["none", "scoped", "direct"] = "scoped"
    read_only: bool = True
    capabilities: tuple[str, ...] = ()
    proxy_flag: str | None = None
    #: tools without a proxy flag (a script driven interpreter) read the standard
    #: HTTP(S)_PROXY variables instead
    proxy_via_env: bool = False
    proxy_scheme: Literal["http", "socks5"] = "http"
    egress_hosts: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()
    #: plain env the tool needs (HOME on the tmpfs, cache dir); secrets go through
    #: env_names so their values never enter the manifest
    env: dict[str, str] = Field(default_factory=dict)
    resolver_url: str | None = None
    execution: ManifestExecution = Field(default_factory=ManifestExecution)
    provision: ManifestProvision = Field(default_factory=ManifestProvision)
    limits: ManifestLimits = Field(default_factory=ManifestLimits)
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    targets: ManifestTargets | None = None
    argv: list[str] = Field(min_length=1)
    stdin_from: str | None = None
    parser: Literal["jsonl", "xml-ports", "text-lines"] = "text-lines"
    summary_limit: int = Field(default=100, ge=1, le=100)
    trusted: bool = False

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _TOOL_ID.fullmatch(value):
            raise ManifestError(f"manifest id must be dotted kebab-case: {value!r}")
        return value

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if not _PINNED_IMAGE.fullmatch(value):
            raise ManifestError(f"manifest image must be pinned by digest: {value!r}")
        return value

    @field_validator("risk_class")
    @classmethod
    def validate_risk(cls, value: str) -> str:
        allowed = {risk.value for risk in RiskClass}
        if value not in allowed:
            raise ManifestError(f"manifest risk_class must be one of {sorted(allowed)}")
        return value

    @field_validator("params")
    @classmethod
    def validate_param_names(cls, value: dict[str, ParamSpec]) -> dict[str, ParamSpec]:
        if len(value) > MAX_PARAMS:
            raise ManifestError(f"at most {MAX_PARAMS} parameters")
        for name in value:
            if not _PARAM_NAME.fullmatch(name):
                raise ManifestError(f"invalid parameter name: {name!r}")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_ARGV:
            raise ManifestError(f"at most {MAX_ARGV} argv tokens")
        for token in value:
            if not token:
                raise ManifestError("argv tokens must not be empty")
            if "\x00" in token:
                raise ManifestError("argv tokens must not contain NUL")
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        for name, item in value.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", name):
                raise ManifestError(f"env name must be upper case: {name!r}")
            if "\n" in item or "\x00" in item:
                raise ManifestError(f"env {name} must be a single line without NUL")
        return value

    @field_validator("proxy_flag")
    @classmethod
    def validate_proxy_flag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"--?[a-z][a-z-]{0,30}", value):
            raise ManifestError(f"proxy_flag must look like '-proxy' or '--proxy': {value!r}")
        return value

    @model_validator(mode="after")
    def validate_integrity(self) -> ToolManifest:
        # an inline script carries its own braces (f-strings, dict literals), so a
        # placeholder is only trusted when the whole token is one; embedded names
        # that are not parameters are left alone by the renderer
        placeholders = {
            name
            for token in self.argv
            for name in _PLACEHOLDER.findall(token)
            if token == "{" + name + "}" or name in self.params
        }
        tokens = {token for token in self.argv}
        unknown = sorted(
            placeholders - set(self.params) if tokens else set()
        )
        if unknown:
            raise ManifestError(f"argv uses unknown parameters: {', '.join(unknown)}")
        if self.stdin_from is not None and self.stdin_from not in self.params:
            raise ManifestError(f"stdin_from names an unknown parameter: {self.stdin_from!r}")
        used = placeholders | ({self.stdin_from} if self.stdin_from else set())
        unused = sorted(set(self.params) - used)
        if unused:
            raise ManifestError(f"parameters used nowhere: {', '.join(unused)}")
        missing = sorted(
            name
            for name, spec in self.params.items()
            if spec.required and name not in placeholders and name != self.stdin_from
        )
        if missing:
            raise ManifestError(f"required parameters never rendered: {', '.join(missing)}")
        for name, spec in self.params.items():
            if not spec.optional:
                continue
            if spec.default is None:
                raise ManifestError(
                    f"optional parameter {name!r} needs a default to fall back to"
                )
            if spec.type != "string":
                raise ManifestError(
                    f"optional parameter {name!r} must be a string (a rendered value)"
                )
        for name in placeholders:
            spec = self.params[name]
            if not spec.required and spec.default is None:
                raise ManifestError(
                    f"parameter {name!r} is rendered but neither required nor defaulted"
                )
        if self.targets is not None:
            if self.targets.param not in self.params:
                raise ManifestError(
                    f"targets.param is not a declared parameter: {self.targets.param!r}"
                )
            spec = self.params[self.targets.param]
            if spec.type not in ("array", "string"):
                raise ManifestError("the targets parameter must be a string or an array")
        if self.risk_class == "r0" and not self.read_only:
            raise ManifestError("r0 manifest must be read-only")
        if self.risk_class in ("r3", "r4") and self.read_only:
            raise ManifestError(f"{self.risk_class} manifest cannot be read-only")
        if self.network == "scoped" and not self.proxy_flag and not self.proxy_via_env:
            raise ManifestError(
                "a scoped manifest needs a proxy flag or proxy_via_env: a tool that "
                "cannot be told about the proxy would reach the network directly"
            )
        if self.network == "none" and (self.proxy_flag or self.egress_hosts):
            raise ManifestError("a network-less manifest cannot declare proxy or hosts")
        if self.resolver_url is not None and self.network != "scoped":
            raise ManifestError("a pinned resolver only makes sense on a scoped manifest")
        return self

    def package_for(self, platform: str) -> PackageSpec | None:
        """The package for a platform family; `macos`/`linux` only, no surprises."""
        return self.provision.packages.get(platform)

    @property
    def capabilities_list(self) -> tuple[str, ...]:
        return self.capabilities

    @property
    def profile_id(self) -> str:
        return f"sandbox.{self.id.replace('.', '-')}@v{self.version}"

    def profile(self) -> SandboxProfile:
        """The sandbox profile this manifest implies (signed like the others)."""
        return SandboxProfile(
            id=self.profile_id,
            description=f"manifest tool {self.id} v{self.version}",
            image=self.image,
            allowed_executables=(self.executable,),
            cpu_limit=self.limits.cpu_limit,
            memory_mb=self.limits.memory_mb,
            pids_limit=self.limits.pids_limit,
            timeout_seconds=self.limits.timeout_seconds,
            max_output_bytes=self.limits.max_output_bytes,
            network_mode=self.network,
            proxy_flag=self.proxy_flag,
            proxy_via_env=self.proxy_via_env,
            proxy_scheme=self.proxy_scheme,
            egress_hosts=self.egress_hosts,
            env_names=self.env_names,
            env=self.env,
            resolver_url=self.resolver_url,
        )

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id=self.id,
            version=self.version,
            kind=ToolKind.SANDBOX,
            description=self.title,
            risk_class=RiskClass(self.risk_class),
            read_only=self.read_only,
            # manifest says "scoped" (profile wording), the definition says proxied
            network=NetworkAccess.PROXIED
            if self.network == "scoped"
            else NetworkAccess(self.network),
            timeout_seconds=self.limits.timeout_seconds,
            output_limit_bytes=self.limits.max_output_bytes,
            parameters=self.json_schema(),
            capabilities=self.capabilities,
        )

    def json_schema(self) -> dict[str, Any]:
        """The fail-closed parameter schema the registry validates against."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, spec in self.params.items():
            entry: dict[str, Any] = {"type": spec.type if spec.type != "array" else "array"}
            if spec.type == "string":
                if spec.enum:
                    entry["enum"] = list(spec.enum)
                if spec.pattern:
                    entry["pattern"] = spec.pattern
                if spec.max_length:
                    entry["maxLength"] = spec.max_length
            elif spec.type == "integer":
                if spec.minimum is not None:
                    entry["minimum"] = spec.minimum
                if spec.maximum is not None:
                    entry["maximum"] = spec.maximum
            elif spec.type == "array":
                items: dict[str, Any] = {"type": "string"}
                if spec.items_enum:
                    items["enum"] = list(spec.items_enum)
                entry["items"] = items
                if spec.max_items:
                    entry["maxItems"] = spec.max_items
            if spec.default is not None:
                entry["default"] = spec.default
            if spec.optional:
                entry["optional"] = True
            properties[name] = entry
            if spec.required:
                required.append(name)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema
