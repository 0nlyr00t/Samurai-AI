"""The shipped example must stay installable — it is the documentation people copy."""

from pathlib import Path

import pytest

from samurai_ai.cli import main
from samurai_ai.services.plugins import validate_package

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "plugins" / "demo-webscan"


def test_the_example_validates() -> None:
    package = validate_package(EXAMPLE)
    assert package.tool_ids == ("demo.webscan",)
    assert package.profile_ids == ("demo.webscan@v1",)
    assert package.playbook_names == ("demo.recon-then-scan",)


def test_the_example_installs_and_removes(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    database = str(tmp_path / "plugins.db")
    assert main(["--db", database, "plugin", "install", str(EXAMPLE), "--trusted"]) == 0
    assert "installed demo 1.0.0" in capsys.readouterr().out
    assert main(["--db", database, "plugin", "rm", "demo", "--yes"]) == 0
    assert "removed demo" in capsys.readouterr().out


@pytest.mark.parametrize("path", ["tools/webscan.json", "profiles/webscan.json"])
def test_editing_an_example_file_needs_a_new_digest(path: str) -> None:
    import hashlib
    import json

    copied = Path(EXAMPLE)
    assert json.loads((copied / path).read_text())
    assert json.loads((copied / "plugin.json").read_text())["files"]
    # the digest in the manifest matches the file on disk right now
    recorded = {
        item["path"]: item["sha256"]
        for item in json.loads((copied / "plugin.json").read_text())["files"]
    }
    assert recorded[path] == hashlib.sha256((copied / path).read_bytes()).hexdigest()
