"""`plugin` through the CLI: install, tamper, check, remove."""

import json
from pathlib import Path

import pytest

from samurai_ai.cli import main

TOOL = {
    "id": "acme.webscan",
    "title": "webscan",
    "image": "ghcr.io/acme/tool@sha256:" + "a" * 64,
    "executable": "webscan",
    "network": "scoped",
    "proxy_flag": "-proxy",
    "argv": ["{target}"],
    "params": {"target": {"type": "string", "required": True}},
    "parser": "text-lines",
}
PROFILE = {
    "id": "acme.webscan@v1",
    "description": "scanner",
    "image": "ghcr.io/acme/tool@sha256:" + "a" * 64,
    "allowed_executables": ["webscan"],
    "network_mode": "scoped",
    "proxy_flag": "-proxy",
}
PLAYBOOK = {"name": "acme.recon", "title": "Acme recon",
            "tasks": [{"id": "t1", "title": "Go"}], "gates": []}


@pytest.fixture
def package(tmp_path: Path) -> Path:
    directory = tmp_path / "pkg"
    for part in ("tools", "profiles", "playbooks"):
        (directory / part).mkdir(parents=True, exist_ok=True)
    import hashlib

    files = {}
    for path, payload, kind in (
        ("tools/webscan.json", TOOL, "tool"),
        ("profiles/webscan.json", PROFILE, "profile"),
        ("playbooks/recon.json", PLAYBOOK, "playbook"),
    ):
        target = directory / path
        target.write_text(json.dumps(payload))
        files[path] = (kind, hashlib.sha256(target.read_bytes()).hexdigest())
    (directory / "plugin.json").write_text(json.dumps({
        "name": "acme", "version": "1.0.0",
        "files": [{"path": p, "kind": k, "sha256": d} for p, (k, d) in files.items()],
    }))
    return directory


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    return str(tmp_path / "plugins.db")


def test_install_then_list_shows_the_package(
    package: Path, database: str, capsys
) -> None:
    assert main(["--db", database, "plugin", "install", str(package), "--trusted"]) == 0
    out = capsys.readouterr().out
    assert "installed acme 1.0.0" in out
    assert main(["--db", database, "plugin", "list"]) == 0
    assert "acme\t1.0.0" in capsys.readouterr().out


def test_install_without_trust_is_refused(package: Path, database: str, capsys) -> None:
    assert main(["--db", database, "plugin", "install", str(package)]) == 2
    assert "--trusted" in capsys.readouterr().err


def test_a_second_install_is_refused(package: Path, database: str, capsys) -> None:
    assert main(["--db", database, "plugin", "install", str(package), "--trusted"]) == 0
    assert main(["--db", database, "plugin", "install", str(package), "--trusted"]) == 2
    assert "already installed" in capsys.readouterr().err


def test_check_reports_a_touched_file(package: Path, database: str, capsys) -> None:
    assert main(["--db", database, "plugin", "install", str(package), "--trusted"]) == 0
    home = Path(__import__("os").environ["SAMURAI_HOME"])
    (home / "plugins" / "acme" / "1.0.0" / "tools" / "webscan.json").write_text("{}")
    capsys.readouterr()
    assert main(["--db", database, "plugin", "check", "acme"]) == 1
    assert "TAMPERED" in capsys.readouterr().out


def test_show_lists_what_the_package_brings(package: Path, database: str, capsys) -> None:
    assert main(["--db", database, "plugin", "install", str(package), "--trusted"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "plugin", "show", "acme"]) == 0
    out = capsys.readouterr().out
    assert "acme.webscan" in out and "acme.recon" in out
    assert "integrity: ok" in out


def test_removal_takes_the_assets_back(package: Path, database: str, capsys) -> None:
    assert main(["--db", database, "plugin", "install", str(package), "--trusted"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "plugin", "rm", "acme", "--yes"]) == 0
    assert "removed acme" in capsys.readouterr().out
    assert main(["--db", database, "plugin", "list"]) == 0
    assert "no plugins installed" in capsys.readouterr().out


def test_removing_an_unknown_plugin_is_an_error(database: str, capsys) -> None:
    assert main(["--db", database, "plugin", "rm", "ghost", "--yes"]) == 1
    assert "not installed" in capsys.readouterr().err


def test_removal_keeps_unrelated_files(package: Path, database: str, capsys) -> None:
    assert main(["--db", database, "plugin", "install", str(package), "--trusted"]) == 0
    home = Path(__import__("os").environ["SAMURAI_HOME"])
    stranger = home / "tools" / "other.tool.json"
    stranger.parent.mkdir(parents=True, exist_ok=True)
    stranger.write_text("{}")
    assert main(["--db", database, "plugin", "rm", "acme", "--yes"]) == 0
    assert stranger.exists()
