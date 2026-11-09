"""Environment detection: read-only, per-probe errors, no side effects."""

from __future__ import annotations

from samurai_ai.domain.environment import (
    Arch,
    EnvironmentDetector,
    OsFamily,
    normalize_arch,
    normalize_os,
    parse_os_release,
)
from samurai_ai.services.fabric import profile_summary


def _detector(
    answers: dict[str, tuple[int, str, str]], names: tuple[str, ...] = ()
) -> EnvironmentDetector:
    """A detector whose probes answer from a table and record what was asked."""

    def runner(argv: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
        return answers.get(argv[0], (127, "", "no such"))

    return EnvironmentDetector(tool_names=names, runner=runner)


def test_os_release_parsing_reads_id_and_version() -> None:
    text = 'NAME="Kali GNU/Linux"\nID=kali\nID_LIKE=debian\nVERSION_ID="2024.4"\n'
    assert parse_os_release(text) == ("kali", "2024.4")
    # quoted, commented and malformed lines never break the parse
    assert parse_os_release('# ID=x\nID="parrot"\nbroken\n') == ("parrot", "")


def test_arch_and_os_normalisation() -> None:
    assert normalize_arch("aarch64") == Arch.ARM64
    assert normalize_arch("x86_64") == Arch.X86_64
    assert normalize_arch("riscv64") == Arch.OTHER
    assert normalize_os("Darwin") == OsFamily.MACOS
    assert normalize_os("Linux") == OsFamily.LINUX
    assert normalize_os("Plan9") == OsFamily.OTHER


def test_a_failing_probe_does_not_stop_the_others() -> None:
    """Detection is a report, not a gate: everything else still gets asked."""
    detector = _detector({"docker": (127, "", "daemon down")})
    profile = detector.detect()
    assert profile.os_family in (OsFamily.LINUX, OsFamily.MACOS, OsFamily.OTHER)
    assert profile.docker_daemon is False
    # one bad probe leaves the rest of the report intact
    assert isinstance(profile.binaries, dict)
    assert profile.free_bytes is not None or profile.free_bytes is None


def test_docker_daemon_version_comes_from_the_probe() -> None:
    detector = _detector({"docker": (0, "27.0.3\n", "")})
    profile = detector.detect()
    assert profile.docker_daemon is True
    assert profile.docker_server_version == "27.0.3"


def test_sudo_is_asked_non_interactively() -> None:
    """`sudo -n true` only: a prompt would hang a detection pass."""
    asked: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
        asked.append(argv)
        return (0, "", "")

    profile = EnvironmentDetector(runner=runner).detect()
    assert ["sudo", "-n", "true"] in asked
    assert profile.sudo_noninteractive is True


def test_detection_is_cached_until_refreshed() -> None:
    calls = {"n": 0}

    def runner(argv: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
        calls["n"] += 1
        return (0, "", "")

    detector = EnvironmentDetector(runner=runner)
    first = detector.detect()
    spent = calls["n"]
    assert detector.detect() is first
    assert calls["n"] == spent
    detector.detect(refresh=True)
    assert calls["n"] > spent


def test_the_summary_names_what_is_missing_and_why_it_matters() -> None:
    profile = _detector({}).detect()
    summary = profile_summary(profile)
    assert summary.offline is False
    assert any("sudo" in note for note in summary.notes)


def test_offline_is_carried_into_the_profile() -> None:
    profile = _detector({}).detect()
    assert profile.offline is False
    offline = EnvironmentDetector(offline=True, runner=lambda *a, **k: (0, "", "")).detect()
    assert offline.offline is True
    assert any("offline" in note for note in profile_summary(offline).notes)
