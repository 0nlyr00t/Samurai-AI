from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.parse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.execution import ToolExecutionError

#: `<namespace>.<name>@v<N>`: the core uses the `sandbox` namespace, a plugin
#: profile carries the plugin's own namespace.
_PROFILE_ID = re.compile(
    r"[a-z][a-z0-9-]*\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?@v[1-9][0-9]*\Z"
)
#: repo@sha256:<digest> from a registry, or a bare local image id (a locally
#: built toolbelt has no registry to pull from until it is pushed).
_PINNED_IMAGE = re.compile(
    r"(?:[a-zA-Z0-9][a-zA-Z0-9._/-]*@|)sha256:[0-9a-f]{64}\Z"
)
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_PROXY_FLAG = re.compile(r"-[a-z][a-z-]{0,31}\Z")
_EGRESS_HOST = re.compile(
    r"(?:\*\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)
MAX_ENV_NAMES = 16
MAX_EGRESS_HOSTS = 64
_PROXY_SCHEMES = ("http", "socks5")
_RESOLVER_URL = re.compile(r"doh:https://[a-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?\Z")
SIGNING_INFO = b"samurai-sandbox-profiles-v1"


class SandboxError(ToolExecutionError):
    pass


class SandboxProfileError(SandboxError):
    pass


class SandboxSignatureError(SandboxError):
    pass


class SandboxProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    description: str = ""
    image: str
    allowed_executables: tuple[str, ...] = Field(min_length=1)
    cpu_limit: str = "1.0"
    memory_mb: int = Field(default=256, ge=16)
    pids_limit: int = Field(default=128, ge=16)
    timeout_seconds: int = Field(default=60, ge=1)
    #: kill the tool after this long without new output (0 disables). Scanners
    #: and browsers stay quiet for minutes, so this is per profile.
    stall_seconds: int = Field(default=120, ge=0)
    max_output_bytes: int = Field(default=65536, ge=1)
    network_mode: str = "none"
    #: container UID:GID; the default drops to nobody, a browser needs root to
    #: reach the build cache under /root/.cache (mode 700 there).
    user: str = "65534:65534"
    # Secret env variables this profile may receive; values never enter argv.
    env_names: tuple[str, ...] = ()
    # Flag through which the tool takes the egress proxy URL, e.g. "-proxy".
    proxy_flag: str | None = None
    # How the proxy URL is addressed: HTTP CONNECT (default) or SOCKS5.
    proxy_scheme: str = "http"
    # DoH resolver the profile is pinned to, in the tool's own "doh:<url>" form.
    resolver_url: str | None = None
    # Third-party hosts the tool may reach besides the operation scope
    # (passive sources); "*.example.com" also matches the apex.
    egress_hosts: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PROFILE_ID.fullmatch(value):
            raise SandboxProfileError(
                f"sandbox profile id must look like <name>.<tool>@v<N>: {value!r}"
            )
        return value

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if not _PINNED_IMAGE.fullmatch(value):
            raise SandboxProfileError(
                "sandbox image must be pinned by digest "
                f"(repo@sha256:... or sha256:...): {value!r}"
            )
        return value

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        if not re.fullmatch(r"\d{1,7}:\d{1,7}", value):
            raise SandboxProfileError(f"sandbox user must look like 65534:65534: {value!r}")
        return value

    @field_validator("network_mode")
    @classmethod
    def validate_network_mode(cls, value: str) -> str:
        if value not in ("none", "scoped", "direct", "host"):
            raise SandboxProfileError(
                f"sandbox network_mode must be 'none', 'scoped', 'direct' or 'host': "
                f"{value!r}"
            )
        return value

    @field_validator("env_names")
    @classmethod
    def validate_env_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_ENV_NAMES:
            raise SandboxProfileError(
                f"sandbox profile accepts at most {MAX_ENV_NAMES} env names"
            )
        if len(set(value)) != len(value):
            raise SandboxProfileError("sandbox profile env names must be unique")
        for name in value:
            if not _ENV_NAME.fullmatch(name):
                raise SandboxProfileError(
                    f"sandbox env name must look like SHODAN_API_KEY: {name!r}"
                )
        return value

    @field_validator("proxy_flag")
    @classmethod
    def validate_proxy_flag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _PROXY_FLAG.fullmatch(value):
            raise SandboxProfileError(
                f"sandbox proxy flag must look like '-proxy': {value!r}"
            )
        return value

    @field_validator("egress_hosts")
    @classmethod
    def validate_egress_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_EGRESS_HOSTS:
            raise SandboxProfileError(
                f"sandbox profile allows at most {MAX_EGRESS_HOSTS} egress hosts"
            )
        if len(set(value)) != len(value):
            raise SandboxProfileError("sandbox egress hosts must be unique")
        for host in value:
            if not _EGRESS_HOST.fullmatch(host):
                raise SandboxProfileError(
                    f"sandbox egress host must be a lowercase host or *.suffix: {host!r}"
                )
        return value

    @field_validator("proxy_scheme")
    @classmethod
    def validate_proxy_scheme(cls, value: str) -> str:
        if value not in _PROXY_SCHEMES:
            raise SandboxProfileError(
                f"sandbox proxy scheme must be one of {_PROXY_SCHEMES}: {value!r}"
            )
        return value

    @field_validator("resolver_url")
    @classmethod
    def validate_resolver_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _RESOLVER_URL.fullmatch(value):
            raise SandboxProfileError(
                f"sandbox resolver must look like doh:https://host/path: {value!r}"
            )
        return value

    def resolver_host(self) -> str | None:
        """Host the pinned resolver talks to, so the profile can allow it."""
        if self.resolver_url is None:
            return None
        _, _, url = self.resolver_url.partition(":")
        return urllib.parse.urlsplit(url).hostname


def canonical_profiles_payload(
    profiles: tuple[SandboxProfile, ...] | list[SandboxProfile],
) -> bytes:
    payload = [
        profile.model_dump(mode="json")
        for profile in sorted(profiles, key=lambda profile: profile.id)
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sandbox_signing_key(master_key: bytes) -> bytes:
    if len(master_key) != 32:
        raise SandboxSignatureError("vault master key must be 32 bytes")
    return hmac.new(master_key, SIGNING_INFO, hashlib.sha256).digest()


def sign_profiles(master_key: bytes, profiles: tuple[SandboxProfile, ...]) -> str:
    return hmac.new(
        sandbox_signing_key(master_key),
        canonical_profiles_payload(profiles),
        hashlib.sha256,
    ).hexdigest()


def verify_profiles(
    master_key: bytes, profiles: tuple[SandboxProfile, ...], signature: str
) -> bool:
    expected = sign_profiles(master_key, profiles)
    return hmac.compare_digest(expected, signature.strip())
