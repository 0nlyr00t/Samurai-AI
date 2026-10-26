import io
import json
import sqlite3
from pathlib import Path

import pytest

from samurai_ai.cli import main
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.vault import VaultRepository

TEST_KEY = "cd" * 32
SECRET_VALUE = "cli-vault-plaintext-9f27"


@pytest.fixture
def vault_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    return tmp_path


def make_vault(database_path: Path, key_path: Path) -> tuple[OperationService, VaultService]:
    database = Database(database_path)
    operations = OperationService(OperationRepository(database))
    vault = VaultService(VaultRepository(database), operations.repository, key_path)
    return operations, vault


def test_vault_persists_across_database_reopen(vault_env: Path) -> None:
    database_path = vault_env / "persist.db"
    key_path = vault_env / "home" / "vault.key"
    operations, vault = make_vault(database_path, key_path)
    operation = operations.create(
        "Persist lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("example.com",))
    )
    metadata = vault.add("api-token", SECRET_VALUE, operation.id)

    _, reopened = make_vault(database_path, key_path)
    assert reopened.get("api-token", operation.id) == SECRET_VALUE
    entries = reopened.list_metadata(operation.id)
    assert [entry.id for entry in entries] == [metadata.id]
    assert entries[0].created_by == "operator"


def test_migration_v4_to_v5_adds_secret_entries(vault_env: Path) -> None:
    database_path = vault_env / "migrated.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for version, script in _MIGRATIONS[:4]:
            connection.executescript(script)
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
        connection.commit()
    finally:
        connection.close()

    Database(database_path).initialize()

    connection = sqlite3.connect(database_path)
    try:
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        assert versions == set(range(1, len(_MIGRATIONS) + 1))
        connection.execute(
            """
            INSERT INTO secret_entries(
                id, operation_id, name, ciphertext, nonce, created_at, created_by
            ) VALUES ('sec_1', NULL, 'token', X'00', X'00', '2026-01-01T00:00:00', 'test')
            """
        )
        connection.commit()
    finally:
        connection.close()


def create_cli_operation(database: str, capsys) -> str:
    result = main(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "Vault CLI",
            "--mode",
            "ctf-htb",
            "--include",
            "example.com",
        ]
    )
    assert result == 0
    return json.loads(capsys.readouterr().out)["id"]


def test_cli_secret_add_list_remove(vault_env: Path, capsys, monkeypatch) -> None:
    database = str(vault_env / "cli.db")
    operation_id = create_cli_operation(database, capsys)

    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET_VALUE + "\n"))
    assert (
        main(
            [
                "--db",
                database,
                "secret",
                "add",
                "api-token",
                "--operation",
                operation_id,
                "--stdin",
            ]
        )
        == 0
    )
    add_output = capsys.readouterr().out
    assert "api-token" in add_output
    assert SECRET_VALUE not in add_output
    assert not (vault_env / "home" / "vault.key").exists()

    assert main(["--db", database, "--json", "secret", "list"]) == 0
    global_entries = json.loads(capsys.readouterr().out)
    assert global_entries == []

    assert (
        main(["--db", database, "--json", "secret", "list", "--operation", operation_id])
        == 0
    )
    entries = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in entries] == ["api-token"]
    assert entries[0]["operation_id"] == operation_id
    assert SECRET_VALUE not in json.dumps(entries)

    assert (
        main(["--db", database, "secret", "remove", "api-token", "--operation", operation_id])
        == 0
    )
    capsys.readouterr()
    assert (
        main(["--db", database, "secret", "remove", "api-token", "--operation", operation_id])
        == 2
    )
    assert "secret not found" in capsys.readouterr().err


def test_cli_secret_add_without_value_on_tty_fails(
    vault_env: Path, capsys, monkeypatch
) -> None:
    database = str(vault_env / "tty.db")

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _Tty())
    assert main(["--db", database, "secret", "add", "api-token"]) == 2
    assert "--value" in capsys.readouterr().err


def test_cli_secret_add_stdin_on_tty_prompts(
    vault_env: Path, capsys, monkeypatch
) -> None:
    database = str(vault_env / "tty-stdin.db")

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    prompts: list[str] = []

    def _fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return SECRET_VALUE

    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("getpass.getpass", _fake_getpass)
    assert main(["--db", database, "secret", "add", "tty-token", "--stdin"]) == 0
    assert prompts == ["Secret value: "]
    output = capsys.readouterr().out
    assert "tty-token" in output
    assert SECRET_VALUE not in output

    assert main(["--db", database, "--json", "secret", "list"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in entries] == ["tty-token"]


def test_cli_tool_run_with_secret_placeholder(vault_env: Path, capsys, monkeypatch) -> None:
    database = str(vault_env / "run.db")
    operation_id = create_cli_operation(database, capsys)
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET_VALUE))
    assert main(["--db", database, "secret", "add", "cli-token", "--stdin"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                database,
                "--json",
                "tool",
                "run",
                operation_id,
                "local.echo",
                "--param",
                "text=prefix {{secret:cli-token}} suffix",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["stdout"] == "prefix ***REDACTED*** suffix"
    assert result["redaction_count"] >= 1
    run_id = result["run"]["id"]
    assert result["run"]["parameters"] == {"text": "prefix {{secret:cli-token}} suffix"}
    assert SECRET_VALUE not in json.dumps(result)

    assert main(["--db", database, "--json", "tool", "show", run_id]) == 0
    shown = capsys.readouterr().out
    assert SECRET_VALUE not in shown
    assert "{{secret:cli-token}}" in shown

    assert main(["--db", database, "--json", "operation", "show", operation_id, "--events"]) == 0
    events_dump = capsys.readouterr().out
    assert SECRET_VALUE not in events_dump

    artifacts_root = vault_env / "artifacts"
    for artifact_file in artifacts_root.rglob("*.txt"):
        assert SECRET_VALUE not in artifact_file.read_text(encoding="utf-8")


def test_cli_tool_run_unknown_secret_fails_before_execution(
    vault_env: Path, capsys
) -> None:
    database = str(vault_env / "fail.db")
    operation_id = create_cli_operation(database, capsys)
    assert (
        main(
            [
                "--db",
                database,
                "tool",
                "run",
                operation_id,
                "local.echo",
                "--param",
                "text={{secret:missing}}",
            ]
        )
        == 2
    )
    assert "cannot resolve secret placeholder" in capsys.readouterr().err

    assert main(["--db", database, "--json", "operation", "show", operation_id, "--events"]) == 0
    events = json.loads(capsys.readouterr().out)["events"]
    assert not [event for event in events if event["event_type"].startswith("tool.run")]


def test_service_execution_with_placeholder_redacts_everywhere(vault_env: Path) -> None:
    database_path = vault_env / "service.db"
    database = Database(database_path)
    operation_repository = OperationRepository(database)
    operations = OperationService(operation_repository)
    kernel = KernelService(operations, KernelRepository(database))
    recovery = RecoveryService(operations, kernel, RecoveryRepository(database))
    vault = VaultService(
        VaultRepository(database), operation_repository, vault_env / "home" / "vault.key"
    )
    execution = ToolExecutionService(
        operations, kernel, recovery, ToolRunRepository(database), vault=vault
    )
    operation = operations.create(
        "Service lab", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    )
    vault.add("token", SECRET_VALUE, operation.id)

    result = execution.run_tool(
        operation.id, "local.echo", {"text": "echo {{secret:token}}"}
    )
    assert result.stdout == "echo ***REDACTED***"
    assert result.run.parameters == {"text": "echo {{secret:token}}"}

    stored = execution.get_run(result.run.id)
    assert stored.parameters == {"text": "echo {{secret:token}}"}
    assert SECRET_VALUE not in stored.model_dump_json()

    for event in operations.events(operation.id):
        assert SECRET_VALUE not in json.dumps(event.payload)

    assert result.run.stdout_artifact_id is not None
    artifact = kernel.kernel.get_artifact(result.run.stdout_artifact_id)
    assert artifact is not None
    content = (database_path.parent / artifact.path).read_text(encoding="utf-8")
    assert SECRET_VALUE not in content
    assert "***REDACTED***" in content
