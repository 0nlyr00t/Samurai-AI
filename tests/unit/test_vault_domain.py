import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.vault import (
    SecretNotFoundError,
    SecretScopeError,
    VaultError,
    secret_references,
    validate_secret_name,
)
from samurai_ai.services.operations import OperationNotFoundError, OperationService
from samurai_ai.services.vault import VaultService, load_master_key
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.vault import SecretConflictError, VaultRepository

TEST_KEY = "ab" * 32


def make_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "vault.db"
) -> tuple[OperationService, VaultService]:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    database = Database(tmp_path / name)
    operations = OperationService(OperationRepository(database))
    vault = VaultService(
        VaultRepository(database),
        operations.repository,
        tmp_path / "vault.key",
    )
    return operations, vault


def make_operation(operations: OperationService, name: str = "Vault lab") -> str:
    return operations.create(
        name, OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    ).id


def test_master_key_from_env_ignores_key_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    key = load_master_key(tmp_path / "vault.key")
    assert key == bytes.fromhex(TEST_KEY)
    assert not (tmp_path / "vault.key").exists()


def test_master_key_file_generated_with_strict_permissions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SAMURAI_VAULT_KEY", raising=False)
    key_path = tmp_path / "home" / "vault.key"
    key = load_master_key(key_path)
    assert len(key) == 32
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert load_master_key(key_path) == key


def test_master_key_rejects_malformed_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "not-hex")
    with pytest.raises(VaultError, match="64 hex"):
        load_master_key(tmp_path / "vault.key")
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 16)
    with pytest.raises(VaultError, match="64 hex"):
        load_master_key(tmp_path / "vault.key")


def test_crypto_roundtrip(tmp_path: Path, monkeypatch) -> None:
    _, vault = make_stack(tmp_path, monkeypatch)
    metadata = vault.add("api-token", "s3cr3t-value")
    assert metadata.operation_id is None
    assert vault.get("api-token") == "s3cr3t-value"


def test_tampered_ciphertext_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _, vault = make_stack(tmp_path, monkeypatch)
    vault.add("api-token", "s3cr3t-value")
    database_path = tmp_path / "vault.db"
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT ciphertext FROM secret_entries WHERE name = 'api-token'"
        ).fetchone()
        tampered = bytes([row[0][0] ^ 0xFF]) + row[0][1:]
        connection.execute(
            "UPDATE secret_entries SET ciphertext = ? WHERE name = 'api-token'",
            (tampered,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(VaultError, match="tampered|wrong master key"):
        vault.get("api-token")


def test_tampered_nonce_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _, vault = make_stack(tmp_path, monkeypatch)
    vault.add("api-token", "s3cr3t-value")
    connection = sqlite3.connect(tmp_path / "vault.db")
    try:
        connection.execute(
            "UPDATE secret_entries SET nonce = ? WHERE name = 'api-token'",
            (b"\x00" * 12,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(VaultError, match="tampered|wrong master key"):
        vault.get("api-token")


def test_scoped_secret_visible_only_to_its_operation(
    tmp_path: Path, monkeypatch
) -> None:
    operations, vault = make_stack(tmp_path, monkeypatch)
    operation_a = make_operation(operations, "Lab A")
    operation_b = make_operation(operations, "Lab B")
    vault.add("token", "scoped-value", operation_a)

    assert vault.get("token", operation_a) == "scoped-value"
    with pytest.raises(SecretScopeError):
        vault.get("token", operation_b)
    with pytest.raises(SecretScopeError):
        vault.get("token")

    vault.add("shared", "global-value")
    assert vault.get("shared", operation_a) == "global-value"
    assert vault.get("shared", operation_b) == "global-value"


def test_add_requires_existing_operation(tmp_path: Path, monkeypatch) -> None:
    _, vault = make_stack(tmp_path, monkeypatch)
    with pytest.raises(OperationNotFoundError):
        vault.add("token", "value", "op_missing")


def test_duplicate_name_in_same_scope_rejected(tmp_path: Path, monkeypatch) -> None:
    operations, vault = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(operations)
    vault.add("token", "one")
    with pytest.raises(SecretConflictError):
        vault.add("token", "two")
    vault.add("token", "scoped", operation_id)
    with pytest.raises(SecretConflictError):
        vault.add("token", "again", operation_id)
    assert vault.get("token", operation_id) == "scoped"
    assert vault.get("token") == "one"


def test_secret_name_validation() -> None:
    assert validate_secret_name("api-token.v2") == "api-token.v2"
    for bad in ("", "Upper", "-leading", "has space", "x" * 65):
        with pytest.raises(VaultError, match="invalid secret name"):
            validate_secret_name(bad)


def test_secret_references_extraction() -> None:
    assert secret_references("no placeholders") == []
    assert secret_references("{{secret:a}} and {{secret:b-c}}") == ["a", "b-c"]
    assert secret_references("{{secret:Invalid}}") == []


def test_resolve_placeholders_returns_plaintexts_for_redactor(
    tmp_path: Path, monkeypatch
) -> None:
    operations, vault = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(operations)
    vault.add("global-token", "global-plain")
    vault.add("scoped-token", "scoped-plain", operation_id)

    resolved, plaintexts = vault.resolve_placeholders(
        "auth {{secret:global-token}} / {{secret:scoped-token}}", operation_id
    )
    assert resolved == "auth global-plain / scoped-plain"
    assert plaintexts == ["global-plain", "scoped-plain"]

    redactor = SecretRedactor()
    for plaintext in plaintexts:
        redactor.register(plaintext)
    result = redactor.redact("output leaked global-plain and scoped-plain")
    assert result.text == "output leaked ***REDACTED*** and ***REDACTED***"
    assert result.count == 2


def test_resolve_placeholders_fail_closed(tmp_path: Path, monkeypatch) -> None:
    operations, vault = make_stack(tmp_path, monkeypatch)
    operation_a = make_operation(operations, "Lab A")
    operation_b = make_operation(operations, "Lab B")
    vault.add("token", "scoped-plain", operation_a)

    with pytest.raises(SecretScopeError):
        vault.resolve_placeholders("{{secret:missing}}", operation_a)
    with pytest.raises(SecretScopeError):
        vault.resolve_placeholders("{{secret:token}}", operation_b)
    unchanged, plaintexts = vault.resolve_placeholders("plain text", operation_a)
    assert unchanged == "plain text"
    assert plaintexts == []


def test_list_returns_metadata_only(tmp_path: Path, monkeypatch) -> None:
    operations, vault = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(operations)
    vault.add("global-token", "global-plain")
    vault.add("scoped-token", "scoped-plain", operation_id)

    global_entries = vault.list_metadata()
    assert [entry.name for entry in global_entries] == ["global-token"]
    visible = vault.list_metadata(operation_id)
    assert [entry.name for entry in visible] == ["global-token", "scoped-token"]
    for entry in visible:
        assert not hasattr(entry, "value")
        assert not hasattr(entry, "ciphertext")


def test_remove_exact_scope(tmp_path: Path, monkeypatch) -> None:
    operations, vault = make_stack(tmp_path, monkeypatch)
    operation_id = make_operation(operations)
    vault.add("token", "global-plain")
    vault.add("token", "scoped-plain", operation_id)

    vault.remove("token", operation_id)
    assert vault.get("token", operation_id) == "global-plain"
    vault.remove("token")
    with pytest.raises(SecretNotFoundError):
        vault.get("token")
