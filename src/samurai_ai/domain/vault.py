from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

_SECRET_NAME = re.compile(r"[a-z0-9](?:[a-z0-9_.-]{0,62}[a-z0-9])?\Z")
SECRET_PLACEHOLDER = re.compile(r"\{\{secret:([a-z0-9](?:[a-z0-9_.-]{0,62}[a-z0-9])?)\}\}")


class VaultError(ValueError):
    pass


class SecretNotFoundError(VaultError):
    pass


class SecretScopeError(VaultError):
    pass


def validate_secret_name(name: str) -> str:
    if not _SECRET_NAME.fullmatch(name):
        raise VaultError(f"invalid secret name: {name!r}")
    return name


def secret_references(text: str) -> list[str]:
    return SECRET_PLACEHOLDER.findall(text)


class SecretMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"sec_{uuid4().hex[:12]}")
    operation_id: str | None = None
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = "operator"
