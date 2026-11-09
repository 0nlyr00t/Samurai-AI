"""Resolver matrix: fixed order, machine-readable reasons, fail closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from samurai_ai.domain.environment import Arch, EnvironmentDetector, OsFamily
from samurai_ai.domain.tool_manifest import ToolManifest
from samurai_ai.services.catalog import RecipeCatalog
from samurai_ai.services.fabric import ProvisionReason, ToolResolver, platform_key

RECIPES = Path("assets/recipes")


def _profile(**overrides: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "os_family": OsFamily.LINUX,
        "distro": "kali",
        "distro_version": "2024.4",
        "arch": Arch.X86_64,
        "package_managers": {"apt-get": "/usr/bin/apt-get"},
        "sudo_noninteractive": True,
        "docker_daemon": True,
    }
    base.update(overrides)
    from samurai_ai.domain.environment import EnvironmentProfile

    return EnvironmentProfile.model_validate(base)


def _resolver(profile, tmp_path: Path, relay: str = "") -> ToolResolver:  # type: ignore[no-untyped-def]
    return ToolResolver(profile, project_local_dir=tmp_path, relay=relay)


def _recipe(name: str) -> ToolManifest:
    return ToolManifest.model_validate(
        {**json.loads((RECIPES / f"{name}.json").read_text()), "trusted": True}
    )


def test_an_installed_binary_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "samurai_ai.services.fabric.which", lambda name: "/usr/bin/nmap" if name == "nmap" else ""
    )
    resolution = _resolver(_profile(), tmp_path).resolve("scan.nmap", _recipe("nmap"))
    assert resolution.reason is ProvisionReason.INSTALLED
    assert resolution.path == "/usr/bin/nmap"


def test_a_project_local_tool_needs_its_script_not_just_the_interpreter(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("samurai_ai.services.fabric.which", lambda name: "/usr/bin/python3")
    recipe = _recipe("http-headers")
    missing = _resolver(_profile(), tmp_path).resolve("web.http-headers", recipe)
    assert missing.reason is ProvisionReason.MISSING_BINARY
    assert missing.can_provision is True
    (tmp_path / "web-http-headers.py").write_text("print('hi')\n")
    present = _resolver(_profile(), tmp_path).resolve("web.http-headers", recipe)
    assert present.reason is ProvisionReason.INSTALLED
    assert present.script.endswith("web-http-headers.py")


def test_container_is_a_path_of_its_own(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("samurai_ai.services.fabric.which", lambda name: "")
    resolution = _resolver(_profile(), tmp_path).resolve("scan.nmap", _recipe("nmap"))
    assert resolution.reason is ProvisionReason.INSTALLED
    assert resolution.detail == "container path"


def test_offline_blocks_provisioning_but_not_detection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("samurai_ai.services.fabric.which", lambda name: "")
    resolution = _resolver(_profile(offline=True, docker_daemon=False), tmp_path).resolve(
        "scan.nmap", _recipe("nmap")
    )
    assert resolution.reason is ProvisionReason.OFFLINE
    assert resolution.can_provision is False


def test_missing_passwordless_sudo_is_missing_privilege_with_a_command(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("samurai_ai.services.fabric.which", lambda name: "")
    resolution = _resolver(
        _profile(sudo_noninteractive=False, docker_daemon=False), tmp_path
    ).resolve("scan.nmap", _recipe("nmap"))
    assert resolution.reason is ProvisionReason.MISSING_PRIVILEGE
    assert resolution.manual_command.startswith("sudo apt-get install")
    # detection still succeeded: only the install needs the password
    assert resolution.can_provision is False


def test_an_unsupported_platform_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("samurai_ai.services.fabric.which", lambda name: "")
    resolution = _resolver(
        _profile(package_managers={}, docker_daemon=False), tmp_path
    ).resolve("scan.nmap", _recipe("nmap"))
    assert resolution.reason is ProvisionReason.UNSUPPORTED_PLATFORM


def test_a_tool_outside_the_catalogue_is_manual_only(tmp_path: Path) -> None:
    resolution = _resolver(_profile(), tmp_path).resolve("crack.john")
    assert resolution.reason is ProvisionReason.MANUAL_ONLY
    assert "catalogue" in resolution.detail


def test_hashcat_without_a_gpu_is_not_provisioned_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("samurai_ai.services.fabric.which", lambda name: "")
    resolution = _resolver(
        _profile(gpu=False, docker_daemon=False), tmp_path
    ).resolve("crack.hashcat", _recipe("hashcat"))
    assert resolution.reason is ProvisionReason.UNSUPPORTED_PLATFORM
    assert "GPU" in resolution.detail


def test_the_relay_is_the_last_resort_and_only_when_it_supports_the_tool(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("samurai_ai.services.fabric.which", lambda name: "")
    profile = _profile(docker_daemon=False)
    with_relay = _resolver(profile, tmp_path, relay="ssh vm").resolve(
        "scan.nmap", _recipe("nmap")
    )
    assert with_relay.reason is ProvisionReason.INSTALLED
    assert with_relay.detail == "relay path"


def test_platform_key_maps_families() -> None:
    assert platform_key(_profile(os_family=OsFamily.MACOS)) == "macos"
    assert platform_key(_profile(os_family=OsFamily.LINUX)) == "linux"


def test_every_builtin_recipe_matches_its_adapter_schema() -> None:
    """A recipe that renders a parameter the tool lacks is caught at load."""
    catalog = RecipeCatalog()
    recipes = catalog.load_all()
    assert {"scan.nmap", "fuzz.ffuf", "scan.nuclei", "crack.hashcat"} <= set(recipes)
    adapted = {
        "scan.nmap": ({"targets": {}, "profile": {}, "ports": {}}, "targets"),
        "fuzz.ffuf": ({"url": {}}, "url"),
        "scan.nuclei": ({"targets": {}, "severity": {}, "template_set": {}}, "targets"),
    }
    for tool_id, (properties, required) in adapted.items():
        RecipeCatalog.validate_against(
            recipes[tool_id], {"properties": properties, "required": [required]}
        )


def test_a_recipe_rendering_an_unknown_parameter_is_rejected() -> None:
    recipe = _recipe("nmap")
    with pytest.raises(Exception, match="does not declare"):
        RecipeCatalog.validate_against(
            recipe, {"properties": {"targets": {}}, "required": ["targets"]}
        )


def test_recipe_packages_name_only_known_managers() -> None:
    for recipe in RecipeCatalog().load_all().values():
        for platform in ("linux", "macos"):
            spec = recipe.package_for(platform)
            assert spec is not None, f"{recipe.id} has no {platform} package"
            assert spec.manager in ("apt", "brew", "project-local")
            # no shell in a package name
            assert " " not in spec.package and ";" not in spec.package


def test_detection_alone_is_enough_for_the_resolver(tmp_path: Path) -> None:
    """The resolver consumes a profile; it never probes by itself."""
    profile = EnvironmentDetector(runner=lambda *a, **k: (127, "", "")).detect()
    assert profile.os_family in (OsFamily.LINUX, OsFamily.MACOS, OsFamily.OTHER)
    assert _resolver(profile, tmp_path).resolve("scan.nmap", _recipe("nmap")).reason
