"""Plan building: dry-run clarity, refusals, and no invented commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from samurai_ai.domain.environment import Arch, EnvironmentProfile, OsFamily
from samurai_ai.domain.provision import (
    ProvisionLockedError,
    ProvisionRefusedError,
    ProvisionStatus,
)
from samurai_ai.services.catalog import RecipeCatalog
from samurai_ai.services.provisioner import Provisioner


class _Operations:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.repository = self

    def append_event(self, event: object) -> None:
        self.events.append(event)


def _profile(**overrides: object) -> EnvironmentProfile:
    base: dict[str, object] = {
        "os_family": OsFamily.LINUX,
        "distro": "kali",
        "distro_version": "2024.4",
        "arch": Arch.X86_64,
        "package_managers": {"apt-get": "/usr/bin/apt-get"},
        "sudo_noninteractive": True,
    }
    base.update(overrides)
    return EnvironmentProfile.model_validate(base)


def _provisioner(tmp_path: Path, **profile: object) -> Provisioner:
    from samurai_ai.execution.process import ProcessManager

    return Provisioner(
        operations=_Operations(),
        kernel=None,
        manager=ProcessManager(allowed_executables=frozenset({"apt-get", "cp", "mkdir"})),
        tools_dir=tmp_path / "tools",
        templates=Path("assets/templates"),
    )


def test_the_plan_names_the_package_the_manager_and_the_exact_argv(tmp_path: Path) -> None:
    plan = _provisioner(tmp_path).plan("scan.nmap", _profile())
    assert plan.package == "nmap"
    assert plan.manager == "apt"
    assert plan.commands == (
        ("sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", "nmap"),
    )
    assert plan.healthcheck == ("nmap", "-V")
    assert plan.privileged is True


def test_brew_platform_never_uses_sudo(tmp_path: Path) -> None:
    plan = _provisioner(tmp_path).plan(
        "scan.nmap", _profile(os_family=OsFamily.MACOS, package_managers={"brew": "/usr/bin/brew"})
    )
    assert plan.commands[0][:2] == ("brew", "install")
    assert "sudo" not in plan.commands[0]


def test_a_project_local_recipe_installs_by_copying(tmp_path: Path) -> None:
    plan = _provisioner(tmp_path).plan("web.http-headers", _profile())
    assert plan.manager == "project-local"
    assert plan.commands[0][:2] == ("mkdir", "-p")
    assert plan.commands[1][0] == "cp"
    assert not plan.privileged


def test_no_shell_metacharacters_in_any_command(tmp_path: Path) -> None:
    provisioner = _provisioner(tmp_path)
    for tool_id in ("scan.nmap", "fuzz.ffuf", "web.http-headers"):
        plan = provisioner.plan(tool_id, _profile())
        for argv in plan.commands:
            for token in argv:
                assert token
                for meta in (";", "&&", "|", ">"):
                    assert not token.startswith(meta)


def test_dry_run_leaves_the_state_untouched(tmp_path: Path) -> None:
    """No events, no status change, no install: only the plan is held."""
    provisioner = _provisioner(tmp_path)
    operations = provisioner.operations
    run = provisioner.run("op_1", "scan.nmap", _profile(), dry_run=True)
    assert run.plan.package == "nmap"
    assert run.status is ProvisionStatus.PLANNED
    assert operations.events == []
    assert provisioner.state("scan.nmap").status is ProvisionStatus.PLANNED


def test_offline_refuses_with_a_reason(tmp_path: Path) -> None:
    with pytest.raises(ProvisionRefusedError) as caught:
        _provisioner(tmp_path).plan("scan.nmap", _profile(offline=True))
    assert caught.value.reason == "offline"


def test_missing_passwordless_sudo_refuses_with_the_manual_command(tmp_path: Path) -> None:
    with pytest.raises(ProvisionRefusedError) as caught:
        _provisioner(tmp_path).plan("scan.nmap", _profile(sudo_noninteractive=False))
    assert caught.value.reason == "missing-privilege"
    assert caught.value.manual.startswith("sudo apt-get install")


def test_a_tool_outside_the_catalogue_refuses(tmp_path: Path) -> None:
    with pytest.raises(ProvisionRefusedError) as caught:
        _provisioner(tmp_path).plan("crack.john", _profile())
    assert caught.value.reason == "manual-only"


def test_an_unknown_platform_refuses(tmp_path: Path) -> None:
    with pytest.raises(ProvisionRefusedError) as caught:
        _provisioner(tmp_path).plan("scan.nmap", _profile(package_managers={}))
    assert caught.value.reason == "unsupported-platform"


def test_hashcat_needs_a_gpu(tmp_path: Path) -> None:
    with pytest.raises(ProvisionRefusedError) as caught:
        _provisioner(tmp_path).plan("crack.hashcat", _profile(gpu=False))
    assert "GPU" in caught.value.detail


def test_a_locked_tool_refuses_a_second_provision(tmp_path: Path) -> None:
    provisioner = _provisioner(tmp_path)
    provisioner.state("scan.nmap").status = ProvisionStatus.INSTALLING
    with pytest.raises(ProvisionLockedError, match="already installing"):
        provisioner.state("scan.nmap").status = ProvisionStatus.INSTALLING
        provisioner.run("op_1", "scan.nmap", _profile())


def test_the_plan_renders_everything_the_operator_must_see(tmp_path: Path) -> None:
    plan = _provisioner(tmp_path).plan("scan.nmap", _profile())
    text = "\n".join(Provisioner.describe(plan))
    assert "nmap" in text
    assert "apt-get install" in text
    assert "sudo -n" in text
    assert "check" in text
    assert "rolls" in text  # reproducibility is stated, not promised


def test_a_failed_attempt_needs_a_new_decision(tmp_path: Path) -> None:
    provisioner = _provisioner(tmp_path)
    provisioner.state("scan.nmap").status = ProvisionStatus.FAILED
    assert provisioner.needs_decision("scan.nmap") is True
    provisioner.state("crack.hashcat")  # untouched
    assert provisioner.needs_decision("crack.hashcat") is False


def test_state_survives_a_restart_and_an_interrupted_lock_is_failed(
    tmp_path: Path,
) -> None:
    from samurai_ai.execution.process import ProcessManager
    from samurai_ai.services.provisioner import Provisioner

    provisioner = _provisioner(tmp_path)
    provisioner.state("scan.nmap").status = ProvisionStatus.READY
    provisioner.state("scan.nmap").installed_version = "7.95"
    provisioner.state("fuzz.ffuf").status = ProvisionStatus.INSTALLING
    provisioner._save_state()

    again = Provisioner(
        operations=_Operations(),
        kernel=None,
        manager=ProcessManager(allowed_executables=frozenset({"apt-get"})),
        tools_dir=tmp_path / "tools",
        templates=Path("assets/templates"),
    )
    assert again.state("scan.nmap").installed_version == "7.95"
    # a lock left behind by a crash must not block forever
    assert again.state("fuzz.ffuf").status is ProvisionStatus.FAILED


def test_the_catalog_is_the_only_source_of_package_names() -> None:
    """Nothing in the plan comes from a caller: only from the recipe file."""
    recipes = RecipeCatalog().load_all()
    for recipe in recipes.values():
        for platform, spec in recipe.provision.packages.items():
            assert spec.package
            assert spec.manager in ("apt", "brew", "project-local")
            assert " " not in spec.package
            assert platform in ("linux", "macos")
    json.dumps({name: recipe.model_dump(mode="json") for name, recipe in recipes.items()})
