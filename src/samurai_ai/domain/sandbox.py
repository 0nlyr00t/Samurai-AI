from __future__ import annotations

import hashlib
import hmac
import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.execution import ToolExecutionError

_PROFILE_ID = re.compile(r"sandbox\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?@v[1-9][0-9]*\Z")
_PINNED_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._/-]*@sha256:[0-9a-f]{64}\Z")
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
    max_output_bytes: int = Field(default=65536, ge=1)
    network_mode: str = "none"

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PROFILE_ID.fullmatch(value):
            raise SandboxProfileError(
                f"sandbox profile id must look like sandbox.<name>@v<N>: {value!r}"
            )
        return value

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if not _PINNED_IMAGE.fullmatch(value):
            raise SandboxProfileError(
                f"sandbox image must be pinned by digest (repo@sha256:...): {value!r}"
            )
        return value

    @field_validator("network_mode")
    @classmethod
    def validate_network_mode(cls, value: str) -> str:
        if value != "none":
            raise SandboxProfileError(
                f"sandbox network_mode {value!r} is not supported yet; only 'none' "
                "is allowed until the egress proxy lands"
            )
        return value


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
