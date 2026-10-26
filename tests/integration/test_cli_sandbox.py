import json
from pathlib import Path

import pytest

from samurai_ai.cli import main

TEST_KEY = "ab" * 32


@pytest.fixture
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    return tmp_path


def create_operation(database: str, capsys) -> str:
    result = main(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "Sandbox CLI",
            "--mode",
            "ctf-htb",
            "--include",
            "example.com",
        ]
    )
    assert result == 0
    return json.loads(capsys.readouterr().out)["id"]


def test_cli_sandbox_sign_and_profiles(sandbox_home: Path, capsys) -> None:
    database = str(sandbox_home / "sandbox.db")
    signature_path = sandbox_home / "home" / "sandbox-allowlist.sig"

    assert main(["--db", database, "sandbox", "profiles"]) == 0
    output = capsys.readouterr().out
    assert "sandbox.default@v1" in output
    assert "sig=unsigned" in output
    assert "busybox@sha256:" in output

    assert main(["--db", database, "sandbox", "sign"]) == 0
    output = capsys.readouterr().out
    assert "signed 4 sandbox profile(s)" in output
    assert signature_path.exists()
    assert signature_path.stat().st_mode & 0o777 == 0o600

    assert main(["--db", database, "--json", "sandbox", "profiles"]) == 0
    profiles = json.loads(capsys.readouterr().out)
    assert profiles[0]["id"] == "sandbox.default@v1"
    assert profiles[0]["signature"] == "signed"
    assert profiles[0]["network_mode"] == "none"
    assert "echo" in profiles[0]["allowed_executables"]

    assert main(["--db", database, "sandbox", "sign"]) == 0
    capsys.readouterr()


def test_cli_sandbox_exec_fail_closed_without_signature(
    sandbox_home: Path, capsys
) -> None:
    database = str(sandbox_home / "run.db")
    operation_id = create_operation(database, capsys)
    assert (
        main(
            [
                "--db",
                database,
                "tool",
                "run",
                operation_id,
                "sandbox.exec",
                "--param",
                "profile=sandbox.default@v1",
                "--param",
                "executable=echo",
            ]
        )
        == 2
    )
    assert "sandbox allowlist signature is missing" in capsys.readouterr().err

    assert (
        main(["--db", database, "--json", "operation", "show", operation_id, "--events"])
        == 0
    )
    events = json.loads(capsys.readouterr().out)["events"]
    assert not [
        event
        for event in events
        if event["event_type"].startswith(("tool.run", "approval"))
    ]


def test_cli_sandbox_exec_rejects_unknown_profile(sandbox_home: Path, capsys) -> None:
    database = str(sandbox_home / "unknown.db")
    operation_id = create_operation(database, capsys)
    assert main(["--db", database, "sandbox", "sign"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                database,
                "tool",
                "run",
                operation_id,
                "sandbox.exec",
                "--param",
                "profile=sandbox.missing@v1",
                "--param",
                "executable=echo",
            ]
        )
        == 2
    )
    assert "unknown sandbox profile" in capsys.readouterr().err


def test_cli_sandbox_exec_rejects_disallowed_executable(
    sandbox_home: Path, capsys
) -> None:
    database = str(sandbox_home / "allowlist.db")
    operation_id = create_operation(database, capsys)
    assert main(["--db", database, "sandbox", "sign"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                database,
                "tool",
                "run",
                operation_id,
                "sandbox.exec",
                "--param",
                "profile=sandbox.default@v1",
                "--param",
                "executable=sh",
            ]
        )
        == 2
    )
    assert "not in sandbox profile" in capsys.readouterr().err
