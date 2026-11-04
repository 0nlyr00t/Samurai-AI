"""Install, integrity and removal of plugin packages against a real database."""

import hashlib
import json
from pathlib import Path

import pytest

from samurai_ai.domain.plugin import PluginError
from samurai_ai.execution.sandbox import load_profile_files
from samurai_ai.services.manifests import ManifestStore
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.plugins import PluginStore, PluginStoreError
from samurai_ai.storage.database import Database
from samurai_ai.storage.playbooks import PlaybookRepository
from samurai_ai.storage.plugins import PluginRepository

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


def package(tmp_path: Path, name: str = "acme") -> Path:
    directory = tmp_path / "pkg"
    for part in ("tools", "profiles", "playbooks"):
        (directory / part).mkdir(parents=True, exist_ok=True)
    files = {}
    for path, payload, kind in (
        ("tools/webscan.json", {**TOOL, "id": f"{name}.webscan"}, "tool"),
        ("profiles/webscan.json", {**PROFILE, "id": f"{name}.webscan@v1"}, "profile"),
        ("playbooks/recon.json", {**PLAYBOOK, "name": f"{name}.recon"}, "playbook"),
    ):
        target = directory / path
        target.write_text(json.dumps(payload))
        files[path] = (kind, hashlib.sha256(target.read_bytes()).hexdigest())
    (directory / "plugin.json").write_text(json.dumps({
        "name": name,
        "version": "1.0.0",
        "files": [{"path": p, "kind": k, "sha256": d} for p, (k, d) in files.items()],
    }))
    return directory


@pytest.fixture
def store(tmp_path: Path) -> PluginStore:
    database = Database(tmp_path / "plugins.db")
    playbooks = PlaybookService.__new__(PlaybookService)
    playbooks.playbooks = PlaybookRepository(database)
    return PluginStore(
        tmp_path / "plugins",
        ManifestStore(tmp_path / "tools"),
        playbooks,
        tmp_path / "profiles",
        PluginRepository(database),
    )


def test_install_registers_everything_the_package_brings(
    tmp_path: Path, store: PluginStore
) -> None:
    installed = store.install(package(tmp_path), trusted=True)
    assert installed.tool_ids == ("acme.webscan",)
    assert (tmp_path / "tools" / "acme.webscan.json").exists()
    assert load_profile_files(tmp_path / "profiles")
    assert store.playbooks.playbooks.get_custom_playbook("acme.recon") is not None
    assert installed.directory.exists()


def test_install_needs_trust(tmp_path: Path, store: PluginStore) -> None:
    with pytest.raises(PluginStoreError, match="--trusted"):
        store.install(package(tmp_path), trusted=False)


def test_a_second_install_of_the_same_plugin_is_refused(
    tmp_path: Path, store: PluginStore
) -> None:
    store.install(package(tmp_path), trusted=True)
    with pytest.raises(PluginStoreError, match="already installed"):
        store.install(package(tmp_path), trusted=True)


def test_a_tool_id_collision_with_an_installed_tool_is_refused(
    tmp_path: Path, store: PluginStore
) -> None:
    store.manifests.install(_write(tmp_path / "loose.json", {**TOOL, "id": "acme.webscan"}),
                            trusted=True)
    with pytest.raises(PluginStoreError, match="already installed"):
        store.install(package(tmp_path), trusted=True)


def test_integrity_reports_an_edited_file(tmp_path: Path, store: PluginStore) -> None:
    installed = store.install(package(tmp_path), trusted=True)
    assert store.integrity("acme") == []
    (installed.directory / "tools/webscan.json").write_text("{}")
    assert store.integrity("acme") == ["tools/webscan.json"]


def test_removal_takes_everything_back(tmp_path: Path, store: PluginStore) -> None:
    store.install(package(tmp_path), trusted=True)
    store.remove("acme")
    assert not (tmp_path / "tools" / "acme.webscan.json").exists()
    assert load_profile_files(tmp_path / "profiles") == ()
    assert store.playbooks.playbooks.get_custom_playbook("acme.recon") is None
    assert store.repository.get("acme") is None
    assert not (tmp_path / "plugins" / "acme").exists()


def test_validate_runs_before_anything_is_copied(tmp_path: Path, store: PluginStore) -> None:
    broken = package(tmp_path)
    (broken / "tools/webscan.json").write_text("{}")
    with pytest.raises(PluginError):
        store.install(broken, trusted=True)
    assert store.repository.list_all() == []


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload))
    return path
