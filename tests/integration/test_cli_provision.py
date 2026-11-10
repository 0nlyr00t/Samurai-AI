"""CLI provision/verify in an isolated home: dry-run, refusals, exit codes."""

from __future__ import annotations

from pathlib import Path

from samurai_ai.cli import main


def test_dry_run_prints_the_plan_and_changes_nothing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SAMURAI_HOME", str(home))
    code = main(["tool", "provision", "scan.nmap", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "package" in out
    assert "dry-run: nothing was changed" in out
    # no tools directory, no install marker, no events
    assert not (home / "tools").exists()


def test_an_unknown_tool_fails_closed(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    assert main(["tool", "provision", "crack.john", "--dry-run"]) == 2
    assert "catalogue" in capsys.readouterr().err


def test_verify_reports_the_version_or_the_refusal(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    assert main(["tool", "verify", "nosuch.tool"]) == 2
    assert "reviewed catalogue" in capsys.readouterr().err


def test_provision_without_an_operation_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    code = main(["tool", "provision", "scan.nmap"])
    assert code == 2
    assert "no operation" in capsys.readouterr().err


def test_the_provision_command_is_advertised_to_the_agent() -> None:
    """The model learns the command and when not to use it."""
    from samurai_ai.agents.orchestrator import AgentOrchestrator

    text = AgentOrchestrator.__dict__
    source = (Path("src/samurai_ai/agents/orchestrator.py")).read_text()
    assert "request_tool_provision" in source
    assert "missing" in source.split("request_tool_provision", 1)[1][:300]
    assert text


def test_the_chat_has_its_own_provision_tool() -> None:
    from samurai_ai.tui import chat as chat_module

    names = {spec.name for spec in chat_module.ChatController._CHAT_TOOL_SPECS}
    assert "provision_tool" in names
    assert "provision_tool" in chat_module.ChatController._CHAT_TOOL_HANDLERS
