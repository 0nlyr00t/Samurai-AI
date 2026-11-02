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
    assert "signed 9 sandbox profile(s)" in output
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


def test_cli_sandbox_exec_signs_a_missing_allowlist(
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
    assert "signature is missing" not in capsys.readouterr().err
    assert (sandbox_home / "home" / "sandbox-allowlist.sig").exists()

    assert (
        main(["--db", database, "--json", "operation", "show", operation_id, "--events"])
        == 0
    )
    events = json.loads(capsys.readouterr().out)["events"]
    # the run now gets past the signature check; without docker on the machine
    # it stops at the docker probe instead of the approval prompt — either way
    # nothing was executed
    assert not [event for event in events if event["event_type"].startswith("tool.run")]


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


def test_cli_sandbox_verify_reports_status(sandbox_home: Path, capsys) -> None:
    database = str(sandbox_home / "verify.db")
    assert main(["--db", database, "sandbox", "verify"]) == 1
    assert "signature is unsigned" in capsys.readouterr().err

    assert main(["--db", database, "sandbox", "sign"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "sandbox", "verify"]) == 0
    assert "signature matches" in capsys.readouterr().out

    # verify never writes: the file stays exactly as signed
    signature_path = sandbox_home / "home" / "sandbox-allowlist.sig"
    before = signature_path.read_text()
    assert main(["--db", database, "sandbox", "verify"]) == 0
    assert signature_path.read_text() == before


def test_cli_sandbox_verify_flags_drift_in_json(sandbox_home: Path, capsys) -> None:
    database = str(sandbox_home / "verify-json.db")
    assert main(["--db", database, "sandbox", "sign"]) == 0
    capsys.readouterr()
    (sandbox_home / "home" / "sandbox-allowlist.sig").write_text("deadbeef\n")
    assert main(["--db", database, "--json", "sandbox", "verify"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "invalid"
    assert payload["ok"] is False


def test_cli_sandbox_profiles_shows_the_last_resign(sandbox_home: Path, capsys) -> None:
    database = str(sandbox_home / "resign.db")
    assert main(["--db", database, "sandbox", "sign"]) == 0
    capsys.readouterr()
    (sandbox_home / "home" / "sandbox-allowlist.sig").unlink()
    # one run with any profile re-signs and records why
    operation_id = create_operation(database, capsys)
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
    capsys.readouterr()
    assert main(["--db", database, "sandbox", "profiles"]) == 0
    out = capsys.readouterr().out
    assert "resigned " in out and "(unsigned," in out
