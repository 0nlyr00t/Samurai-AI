from __future__ import annotations

import os
import re
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from samurai_ai.domain.vault import (
    SECRET_PLACEHOLDER,
    SecretMetadata,
    SecretNotFoundError,
    SecretScopeError,
    VaultError,
    secret_references,
    validate_secret_name,
)
from samurai_ai.services.operations import OperationNotFoundError
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.vault import SecretEntry, VaultRepository

_KEY_ENV_VAR = "SAMURAI_VAULT_KEY"
_KEY_BYTES = 32
_NONCE_BYTES = 12


def load_master_key(key_path: Path) -> bytes:
    configured = os.environ.get(_KEY_ENV_VAR)
    if configured is not None:
        try:
            key = bytes.fromhex(configured)
        except ValueError as exc:
            raise VaultError(f"{_KEY_ENV_VAR} must be 64 hex characters") from exc
        if len(key) != _KEY_BYTES:
            raise VaultError(f"{_KEY_ENV_VAR} must be 64 hex characters")
        return key
    if key_path.exists():
        key = key_path.read_bytes()
        if len(key) != _KEY_BYTES:
            raise VaultError(f"vault key file is corrupted: {key_path}")
        return key
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(_KEY_BYTES)
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, key)
    finally:
        os.close(descriptor)
    return key


class VaultService:
    def __init__(
        self,
        repository: VaultRepository,
        operations: OperationRepository,
        key_path: Path,
    ) -> None:
        self.repository = repository
        self.operations = operations
        self.key_path = key_path
        self._key: bytes | None = None

    def add(
        self,
        name: str,
        value: str,
        operation_id: str | None = None,
        *,
        created_by: str = "operator",
    ) -> SecretMetadata:
        validate_secret_name(name)
        if not value:
            raise VaultError("secret value must not be empty")
        if operation_id is not None and self.operations.get(operation_id) is None:
            raise OperationNotFoundError(f"operation not found: {operation_id}")
        metadata = SecretMetadata(
            operation_id=operation_id, name=name, created_by=created_by
        )
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher().encrypt(nonce, value.encode("utf-8"), None)
        self.repository.add(
            SecretEntry(metadata=metadata, ciphertext=ciphertext, nonce=nonce)
        )
        return metadata

    def get(self, name: str, operation_id: str | None = None) -> str:
        entry = self.repository.find_visible(name, operation_id)
        if entry is None:
            if self.repository.find_any(name) is not None:
                scope = operation_id or "global"
                raise SecretScopeError(
                    f"secret {name!r} is not visible in scope {scope}"
                )
            raise SecretNotFoundError(f"secret not found: {name!r}")
        return self._decrypt(entry)

    def remove(self, name: str, operation_id: str | None = None) -> None:
        if not self.repository.delete(name, operation_id):
            scope = operation_id or "global"
            raise SecretNotFoundError(
                f"secret not found in scope {scope}: {name!r}"
            )

    def exists(self, name: str, operation_id: str | None = None) -> bool:
        return self.repository.find_visible(name, operation_id) is not None

    def set(
        self, name: str, value: str, operation_id: str | None = None
    ) -> SecretMetadata:
        """Replace an existing secret's value in place (its scope stays)."""
        self.get(name, operation_id)
        self.remove(name, operation_id)
        return self.add(name, value, operation_id)

    def list_metadata(self, operation_id: str | None = None) -> list[SecretMetadata]:
        if operation_id is not None and self.operations.get(operation_id) is None:
            raise OperationNotFoundError(f"operation not found: {operation_id}")
        return self.repository.list_metadata(operation_id)

    def resolve_placeholders(
        self, text: str, operation_id: str | None
    ) -> tuple[str, list[str]]:
        if not secret_references(text):
            return text, []
        plaintexts: list[str] = []

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                value = self.get(name, operation_id)
            except VaultError as exc:
                raise SecretScopeError(
                    f"cannot resolve secret placeholder {name!r} "
                    f"for operation {operation_id or 'global'}"
                ) from exc
            plaintexts.append(value)
            return value

        resolved = SECRET_PLACEHOLDER.sub(replace, text)
        return resolved, plaintexts

    def _decrypt(self, entry: SecretEntry) -> str:
        try:
            plaintext = self._cipher().decrypt(entry.nonce, entry.ciphertext, None)
        except InvalidTag as exc:
            raise VaultError(
                f"cannot decrypt secret {entry.metadata.name!r}: "
                "tampered ciphertext or wrong master key"
            ) from exc
        return plaintext.decode("utf-8")

    def _cipher(self) -> AESGCM:
        if self._key is None:
            self._key = load_master_key(self.key_path)
        return AESGCM(self._key)
