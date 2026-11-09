"""Provision execution against a fake package manager: argv, gates, verdicts."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.environment import Arch, EnvironmentProfile, OsFamily
from samurai_ai.domain.provision import (
    ProvisionLockedError,
    ProvisionStatus,
)
from samurai_ai.execution.process import ProcessResult
from samurai_ai.services.provisioner import Provisioner


class _Operations:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.repository = self

    def append_event(self, event: object) -> None:
        self.events.append(event)

    def event_types(self) -> list[str]:
        return [getattr(event, "event_type", "") for event in self.events]


def _profile(**overrides: object) -> EnvironmentProfile:
    base: dict[str, object] = {
        "os_family": OsFamily.LINUX,
        "distro": "kali",
        "arch": Arch.X86_64,
        "package_managers": {"apt-get": "/usr/bin/apt-get"},
        "sudo_noninteractive": True,
    }
    base.update(overrides)
    return EnvironmentProfile.model_validate(base)


class FakeManager:
    """Answers by argv[0] and records every command in order."""

    def __init__(self, answers: dict[str, tuple[int, str, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> ProcessResult:
        self.calls.append(list(argv))
        # `sudo -n` is transport: the fake answers for the real command
        command = argv[2:] if argv[:1] == ["sudo"] else argv
        code, out, err = self.answers.get(command[0].rsplit("/", 1)[-1], (0, "", ""))
        return ProcessResult(exit_code=code, stdout=out, stderr=err)


def _provisioner(
    tmp_path: Path, answers: dict[str, tuple[int, str, str]]
) -> tuple[Provisioner, FakeManager]:
    from samurai_ai.execution.process import ProcessManager

    manager = FakeManager(answers)
    provisioner = Provisioner(
        operations=_Operations(),
        kernel=None,
        manager=ProcessManager(allowed_executables=frozenset({"apt-get", "cp", "mkdir"})),
        tools_dir=tmp_path / "tools",
        templates=Path("assets/templates"),
        runner=manager,
    )
    return provisioner, manager


def test_a_successful_install_runs_the_plan_and_verifies(tmp_path: Path) -> None:
    provisioner, manager = _provisioner(
        tmp_path, {"apt-get": (0, "installed", ""), "nmap": (0, "Nmap version 7.95", "")}
    )
    run = provisioner.run("op_1", "scan.nmap", _profile())
    assert run.status is ProvisionStatus.READY
    assert manager.calls[0][:3] == ["sudo", "-n", "apt-get"]
    assert manager.calls[0][3:] == ["install", "-y", "--no-install-recommends", "nmap"]
    assert manager.calls[-1][0].rsplit("/", 1)[-1] == "nmap"
    assert manager.calls[-1][1:] == ["-V"]
    assert provisioner.state("scan.nmap").ready is True
    assert "provision.started" in provisioner.operations.event_types()
    assert "provision.ready" in provisioner.operations.event_types()


def test_a_nonzero_exit_stops_before_the_healthcheck(tmp_path: Path) -> None:
    provisioner, manager = _provisioner(
        tmp_path, {"apt-get": (100, "", "E: unable to locate package nmap")}
    )
    run = provisioner.run("op_1", "scan.nmap", _profile())
    assert run.status is ProvisionStatus.FAILED
    assert "unable to locate" in run.error
    # the healthcheck never ran: a failed install is not verified
    assert all(call[0].rsplit("/", 1)[-1] != "nmap" for call in manager.calls)
    assert provisioner.state("scan.nmap").ready is False
    assert "provision.failed" in provisioner.operations.event_types()


def test_a_failing_healthcheck_is_not_ready(tmp_path: Path) -> None:
    """A partially installed package exits 0 and still cannot be used."""
    provisioner, _manager = _provisioner(
        tmp_path, {"apt-get": (0, "", ""), "nmap": (127, "", "nmap: not found")}
    )
    run = provisioner.run("op_1", "scan.nmap", _profile())
    assert run.status is ProvisionStatus.FAILED
    assert "healthcheck failed" in run.error
    assert provisioner.state("scan.nmap").status is ProvisionStatus.FAILED


def test_a_second_provision_of_a_locked_tool_refuses(tmp_path: Path) -> None:
    provisioner, _manager = _provisioner(tmp_path, {})
    provisioner.state("scan.nmap").status = ProvisionStatus.INSTALLING
    with pytest.raises(ProvisionLockedError):
        provisioner.run("op_1", "scan.nmap", _profile())


def test_an_approval_is_required_and_reported(tmp_path: Path) -> None:
    provisioner, manager = _provisioner(tmp_path, {})
    with pytest.raises(Exception, match="needs an approval"):
        provisioner.run("op_1", "scan.nmap", _profile(), approved=False)
    assert manager.calls == []
    assert provisioner.state("scan.nmap").status is ProvisionStatus.AWAITING_APPROVAL


def test_a_project_local_install_copies_the_template(tmp_path: Path) -> None:
    provisioner, manager = _provisioner(tmp_path, {"cp": (0, "", "")})

    def _copy(argv: list[str], timeout: float) -> ProcessResult:
        manager.calls.append(list(argv))
        if argv[0] == "cp":
            Path(argv[2]).parent.mkdir(parents=True, exist_ok=True)
            Path(argv[2]).write_text(Path(argv[1]).read_text())
        return ProcessResult(exit_code=0, stdout="", stderr="")

    provisioner._runner = _copy
    _patch_which(monkeypatch_=None, name="python3", value="/usr/bin/python3")
    run = provisioner.run("op_1", "web.http-headers", _profile())
    installed = tmp_path / "tools" / "bin" / "web-http-headers.py"
    assert installed.exists()
    assert run.status is ProvisionStatus.READY


def _patch_which(*, monkeypatch_: object, name: str, value: str) -> None:
    import samurai_ai.services.provisioner as module

    original = module.shutil.which
    module.shutil.which = lambda candidate: value if candidate == name else original(candidate)
    _RESTORE.append(original)


_RESTORE: list[object] = []


@pytest.fixture(autouse=True)
def _restore_which():  # type: ignore[no-untyped-def]
    yield
    import samurai_ai.services.provisioner as module

    while _RESTORE:
        module.shutil.which = _RESTORE.pop()  # type: ignore[assignment]


def test_the_healthcheck_output_becomes_the_recorded_version(tmp_path: Path) -> None:
    provisioner, _manager = _provisioner(
        tmp_path, {"apt-get": (0, "", ""), "nmap": (0, "Nmap version 7.95\nmore", "")}
    )
    run = provisioner.run("op_1", "scan.nmap", _profile())
    assert run.status is ProvisionStatus.READY
    assert provisioner.state("scan.nmap").installed_version == "Nmap version 7.95"


def test_the_logs_carry_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTB_TOKEN", "super-secret-token")
    provisioner, _manager = _provisioner(tmp_path, {"apt-get": (0, "", "")})
    provisioner.run("op_1", "scan.nmap", _profile())
    import json

    payloads = [
        json.dumps(getattr(event, "payload", {}), default=str)
        for event in provisioner.operations.events
    ]
    assert all("super-secret-token" not in payload for payload in payloads)
