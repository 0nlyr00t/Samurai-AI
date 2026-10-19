import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from samurai_ai.domain.execution import ToolExecutionError, ToolValidationError
from samurai_ai.domain.sandbox import (
    SandboxProfile,
    SandboxSignatureError,
    canonical_profiles_payload,
    sign_profiles,
    verify_profiles,
)
from samurai_ai.execution.process import ProcessResult
from samurai_ai.execution.sandbox import (
    SANDBOX_PROFILES,
    DockerSandboxRunner,
    get_profile,
    sign_allowlist,
)

MASTER_KEY = bytes.fromhex("ab" * 32)
OTHER_KEY = bytes.fromhex("cd" * 32)


def make_profile(**overrides: object) -> SandboxProfile:
    payload: dict[str, object] = {
        "id": "sandbox.test@v1",
        "image": "busybox@sha256:" + "0" * 64,
        "allowed_executables": ("echo",),
    }
    payload.update(overrides)
    return SandboxProfile.model_validate(payload)


class FakeManager:
    def __init__(self, results: dict[tuple[str, ...], ProcessResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, ...]] = []

    async def run(
        self, argv: list[str] | tuple[str, ...], timeout_seconds: float | None = None
    ) -> ProcessResult:
        arguments = tuple(argv)
        self.calls.append(arguments)
        if arguments[:2] == ("docker", "version"):
            return self.results.get(
                ("docker", "version"), ProcessResult(exit_code=0, stdout="27.0.0", stderr="")
            )
        if arguments[:2] == ("docker", "image"):
            return self.results.get(
                ("docker", "image"), ProcessResult(exit_code=0, stdout="[]", stderr="")
            )
        return self.results.get(
            ("docker", "run"), ProcessResult(exit_code=0, stdout="hello", stderr="")
        )


def make_runner(
    tmp_path: Path, manager: FakeManager | None = None
) -> tuple[DockerSandboxRunner, FakeManager]:
    fake = manager or FakeManager()
    runner = DockerSandboxRunner(
        fake, tmp_path / "sandbox-allowlist.sig", lambda: MASTER_KEY
    )
    return runner, fake


def sign_default(tmp_path: Path) -> None:
    sign_allowlist(tmp_path / "sandbox-allowlist.sig", MASTER_KEY)


def test_profile_requires_versioned_id() -> None:
    with pytest.raises(ValidationError, match="sandbox.<name>@v"):
        make_profile(id="sandbox.test")
    with pytest.raises(ValidationError, match="sandbox.<name>@v"):
        make_profile(id="default@v1")
    assert make_profile(id="sandbox.default@v2").id == "sandbox.default@v2"


def test_profile_requires_digest_pinned_image() -> None:
    for bad in ("busybox:latest", "busybox", "busybox@sha256:abc", "busybox@sha256:" + "0" * 63):
        with pytest.raises(ValidationError, match="pinned by digest"):
            make_profile(image=bad)
    assert make_profile(image="repo/name@sha256:" + "f" * 64).image.startswith("repo/name@")


def test_profile_network_mode_fail_closed() -> None:
    for bad in ("scoped", "bridge", "host"):
        with pytest.raises(ValidationError, match="only 'none'"):
            make_profile(network_mode=bad)
    assert make_profile().network_mode == "none"


def test_canonical_payload_is_deterministic_and_order_independent() -> None:
    first = canonical_profiles_payload(list(SANDBOX_PROFILES))
    second = canonical_profiles_payload(list(reversed(SANDBOX_PROFILES)))
    assert first == second
    profile = make_profile()
    assert canonical_profiles_payload([profile]) == canonical_profiles_payload([profile])


def test_sign_verify_roundtrip_and_tamper_detection() -> None:
    signature = sign_profiles(MASTER_KEY, SANDBOX_PROFILES)
    assert verify_profiles(MASTER_KEY, SANDBOX_PROFILES, signature)
    assert verify_profiles(MASTER_KEY, SANDBOX_PROFILES, signature + "\n")
    assert not verify_profiles(OTHER_KEY, SANDBOX_PROFILES, signature)

    tampered = SandboxProfile(
        **{**SANDBOX_PROFILES[0].model_dump(), "memory_mb": 1024}
    )
    assert not verify_profiles(MASTER_KEY, (tampered,), signature)
    assert not verify_profiles(MASTER_KEY, (), signature)


def test_runner_builds_hardened_argv(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)
    profile = get_profile("sandbox.default@v1")
    argv = runner.build_argv(profile, "run_abc123", "echo", ["hi there"])
    assert argv[0] == "docker"
    assert argv[1] == "run"
    for flag in (
        "--rm",
        "--read-only",
        "--tmpfs",
        "--cap-drop",
        "--security-opt",
        "--user",
        "--cpus",
        "--memory",
        "--pids-limit",
    ):
        assert flag in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert argv[argv.index("--name") + 1] == "samurai-run_abc123"
    assert argv[-3:] == [profile.image, "echo", "hi there"]


def test_runner_rejects_executable_outside_profile_allowlist(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)
    profile = get_profile("sandbox.default@v1")
    with pytest.raises(ToolValidationError, match="not in sandbox profile"):
        runner.build_argv(profile, "run_1", "sh", ["-c", "id"])
    with pytest.raises(ToolValidationError, match="not in sandbox profile"):
        runner.build_argv(profile, "run_1", "/bin/echo", [])


def test_unknown_profile_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolValidationError, match="unknown sandbox profile"):
        get_profile("sandbox.missing@v9")


def test_preflight_fails_without_signature(tmp_path: Path) -> None:
    runner, fake = make_runner(tmp_path)
    profile = get_profile("sandbox.default@v1")
    with pytest.raises(SandboxSignatureError, match="signature is missing"):
        asyncio.run(runner.preflight(profile))
    assert fake.calls == []


def test_preflight_fails_with_invalid_signature(tmp_path: Path) -> None:
    sign_allowlist(tmp_path / "sandbox-allowlist.sig", OTHER_KEY)
    runner, fake = make_runner(tmp_path)
    profile = get_profile("sandbox.default@v1")
    with pytest.raises(SandboxSignatureError, match="signature is invalid"):
        asyncio.run(runner.preflight(profile))
    assert fake.calls == []


def test_preflight_fails_when_docker_unavailable(tmp_path: Path) -> None:
    sign_default(tmp_path)
    manager = FakeManager(
        {("docker", "version"): ProcessResult(exit_code=1, stdout="", stderr="boom")}
    )
    runner, _ = make_runner(tmp_path, manager)
    profile = get_profile("sandbox.default@v1")
    with pytest.raises(ToolExecutionError, match="docker is not available"):
        asyncio.run(runner.preflight(profile))


def test_preflight_fails_when_image_missing(tmp_path: Path) -> None:
    sign_default(tmp_path)
    manager = FakeManager(
        {("docker", "image"): ProcessResult(exit_code=1, stdout="", stderr="no such image")}
    )
    runner, _ = make_runner(tmp_path, manager)
    profile = get_profile("sandbox.default@v1")
    with pytest.raises(ToolExecutionError, match="not present locally"):
        asyncio.run(runner.preflight(profile))


def test_run_executes_full_argv_via_manager(tmp_path: Path) -> None:
    sign_default(tmp_path)
    runner, fake = make_runner(tmp_path)
    profile = get_profile("sandbox.default@v1")
    result = runner.run(profile, "run_xyz", "echo", ["hello sandbox"])
    assert result.exit_code == 0
    assert result.stdout == "hello"
    run_calls = [call for call in fake.calls if call[:2] == ("docker", "run")]
    assert len(run_calls) == 1
    assert run_calls[0][-2:] == ("echo", "hello sandbox")


def test_run_enforces_profile_output_limit(tmp_path: Path) -> None:
    sign_default(tmp_path)
    manager = FakeManager(
        {("docker", "run"): ProcessResult(exit_code=0, stdout="x" * 100000, stderr="")}
    )
    runner, _ = make_runner(tmp_path, manager)
    profile = get_profile("sandbox.default@v1")
    result = runner.run(profile, "run_big", "cat", ["big.txt"])
    assert len(result.stdout.encode("utf-8")) <= profile.max_output_bytes
    assert result.truncated


def test_sign_allowlist_writes_signature_file(tmp_path: Path) -> None:
    signature = sign_allowlist(tmp_path / "home" / "sandbox-allowlist.sig", MASTER_KEY)
    path = tmp_path / "home" / "sandbox-allowlist.sig"
    assert path.read_text(encoding="ascii").strip() == signature
    assert path.stat().st_mode & 0o777 == 0o600
