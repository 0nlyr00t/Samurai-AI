"""`samurai tool capabilities` in an isolated home: read-only, JSON, exit 0."""

from __future__ import annotations

import json
from pathlib import Path

from samurai_ai.cli import main


def test_capabilities_reports_the_machine_without_touching_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SAMURAI_HOME", str(home))
    assert main(["tool", "capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["os_family"]
    assert payload["arch"]
    assert isinstance(payload["tools"], list)
    # the CLI builds its stack, so the base exists; detection itself adds no
    # tool state and installs nothing
    assert not (home / "tools").exists()
    import sqlite3

    with sqlite3.connect(home / "samurai.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0] == 0


def test_capabilities_default_output_is_one_line_per_tool(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    assert main(["tool", "capabilities"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("os\t")
