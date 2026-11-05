"""A plugin package is refused before anything is copied."""

import hashlib
import json
from pathlib import Path

import pytest

from samurai_ai.domain.plugin import PluginError, PluginManifest
from samurai_ai.services.plugins import validate_package

DIGEST = "0" * 64
GOOD_PROFILE = {
    "id": "acme.webscan@v1",
    "description": "scanner",
    "image": "ghcr.io/acme/tool@sha256:" + "a" * 64,
    "allowed_executables": ["webscan"],
    "network_mode": "scoped",
    "proxy_flag": "-proxy",
}
GOOD_TOOL = {
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
GOOD_PLAYBOOK = {
    "name": "acme.recon",
    "title": "Acme recon",
    "tasks": [{"id": "t1", "title": "Go"}],
    "gates": [],
}


def write(directory: Path, name: str, payload: object) -> None:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(tmp_path: Path, *, tool: dict | None = None, profile: dict | None = None,
          playbook: dict | None = None) -> Path:
    directory = tmp_path / "pkg"
    directory.mkdir()
    write(directory, "tools/webscan.json", tool or GOOD_TOOL)
    write(directory, "profiles/webscan.json", profile or GOOD_PROFILE)
    write(directory, "playbooks/recon.json", playbook or GOOD_PLAYBOOK)
    manifest = {
        "name": "acme",
        "version": "1.0.0",
        "files": [
            {"path": "tools/webscan.json", "kind": "tool",
             "sha256": digest(directory / "tools/webscan.json")},
            {"path": "profiles/webscan.json", "kind": "profile",
             "sha256": digest(directory / "profiles/webscan.json")},
            {"path": "playbooks/recon.json", "kind": "playbook",
             "sha256": digest(directory / "playbooks/recon.json")},
        ],
    }
    write(directory, "plugin.json", manifest)
    return directory


def test_a_good_package_validates(tmp_path: Path) -> None:
    package = validate_package(build(tmp_path))
    assert package.tool_ids == ("acme.webscan",)
    assert package.profile_ids == ("acme.webscan@v1",)
    assert package.playbook_names == ("acme.recon",)


def test_an_unlisted_file_is_refused(tmp_path: Path) -> None:
    directory = build(tmp_path)
    (directory / "stray.json").write_text("{}")
    with pytest.raises(PluginError, match="not listed"):
        validate_package(directory)


def test_a_changed_file_is_refused(tmp_path: Path) -> None:
    directory = build(tmp_path)
    (directory / "tools/webscan.json").write_text(json.dumps({**GOOD_TOOL, "title": "x"}))
    with pytest.raises(PluginError, match="sha256 does not match"):
        validate_package(directory)


def test_a_tool_without_the_plugin_prefix_is_refused(tmp_path: Path) -> None:
    directory = build(tmp_path, tool={**GOOD_TOOL, "id": "other.webscan"})
    with pytest.raises(PluginError, match="must start with the plugin name"):
        validate_package(directory)


def test_a_profile_without_the_plugin_prefix_is_refused(tmp_path: Path) -> None:
    directory = build(tmp_path, profile={**GOOD_PROFILE, "id": "sandbox.webscan@v1"})
    with pytest.raises(PluginError, match="must start with the plugin name"):
        validate_package(directory)


def test_a_scoped_profile_needs_a_way_to_the_proxy(tmp_path: Path) -> None:
    profile = {**GOOD_PROFILE, "proxy_flag": None}
    directory = build(tmp_path, profile=profile)
    with pytest.raises(PluginError, match="proxy flag or proxy_via_env"):
        validate_package(directory)
    # a script-driven tool reads HTTP(S)_PROXY instead of taking a flag
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    ok = {**GOOD_PROFILE, "proxy_flag": None, "proxy_via_env": True}
    (directory / "profiles/webscan.json").write_text(json.dumps(ok))
    manifest_path = directory / "plugin.json"
    raw = json.loads(manifest_path.read_text())
    for item in raw["files"]:
        if item["path"] == "profiles/webscan.json":
            item["sha256"] = hashlib.sha256(
                (directory / "profiles/webscan.json").read_bytes()
            ).hexdigest()
    manifest_path.write_text(json.dumps(raw))
    validate_package(directory)


def test_a_root_container_must_explain_itself(tmp_path: Path) -> None:
    directory = build(tmp_path, profile={**GOOD_PROFILE, "user": "0:0", "description": ""})
    with pytest.raises(PluginError, match="must say why"):
        validate_package(directory)


def test_a_plugin_without_tools_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "only-playbooks"
    directory.mkdir()
    write(directory, "pb.json", GOOD_PLAYBOOK)
    write(directory, "plugin.json", {
        "name": "acme", "version": "1.0.0",
        "files": [{"path": "pb.json", "kind": "playbook",
                   "sha256": digest(directory / "pb.json")}],
    })
    with pytest.raises(PluginError, match="at least one tool"):
        validate_package(directory)


def test_a_missing_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="no plugin.json"):
        validate_package(tmp_path)


def test_the_manifest_rejects_bad_names_versions_and_paths() -> None:
    with pytest.raises(Exception, match="kebab-case"):
        PluginManifest(name="Acme_Tools", version="1.0.0",
                       files=[{"path": "a.json", "sha256": DIGEST, "kind": "tool"}])
    with pytest.raises(Exception, match="1.2.3"):
        PluginManifest(name="acme", version="v1",
                       files=[{"path": "a.json", "sha256": DIGEST, "kind": "tool"}])
    with pytest.raises(Exception, match="relative"):
        PluginManifest(name="acme", version="1.0.0",
                       files=[{"path": "/a.json", "sha256": DIGEST, "kind": "tool"}])
    with pytest.raises(Exception, match="dotted"):
        PluginManifest(name="acme", version="1.0.0",
                       files=[{"path": "../a.json", "sha256": DIGEST, "kind": "tool"}])
