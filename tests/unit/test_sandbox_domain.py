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
from samurai_ai.domain.scope import ScopeContract
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
        self.envs: list[dict[str, str] | None] = []

    async def run(
        self,
        argv: list[str] | tuple[str, ...],
        timeout_seconds: float | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        arguments = tuple(argv)
        self.calls.append(arguments)
        self.envs.append(dict(env) if env is not None else None)
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
    for bad in ("bridge", "host", "Scoped"):
        with pytest.raises(ValidationError, match="'none' or 'scoped'"):
            make_profile(network_mode=bad)
    assert make_profile().network_mode == "none"
    assert make_profile(network_mode="scoped").network_mode == "scoped"


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
    assert argv[-4:] == ["--entrypoint", "echo", profile.image, "hi there"]


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


def test_profile_validates_env_channel() -> None:
    with pytest.raises(ValidationError, match="SHODAN_API_KEY"):
        make_profile(env_names=("shodan_key",))
    with pytest.raises(ValidationError, match="unique"):
        make_profile(env_names=("A_KEY", "A_KEY"))
    with pytest.raises(ValidationError, match="at most"):
        make_profile(env_names=tuple(f"K{index}_API_KEY" for index in range(17)))
    with pytest.raises(ValidationError, match="proxy flag"):
        make_profile(proxy_flag="proxy")
    assert make_profile(env_names=("A_KEY",), proxy_flag="-proxy").env_names == (
        "A_KEY",
    )


def test_profile_validates_egress_hosts() -> None:
    assert make_profile(egress_hosts=("crt.sh", "*.commoncrawl.org")).egress_hosts == (
        "crt.sh",
        "*.commoncrawl.org",
    )
    with pytest.raises(ValidationError, match="lowercase host"):
        make_profile(egress_hosts=("CRT.SH",))
    with pytest.raises(ValidationError, match="lowercase host"):
        make_profile(egress_hosts=("https://crt.sh",))
    with pytest.raises(ValidationError, match="unique"):
        make_profile(egress_hosts=("crt.sh", "crt.sh"))
    with pytest.raises(ValidationError, match="at most"):
        make_profile(egress_hosts=tuple(f"h{index}.example.org" for index in range(65)))


def test_runner_puts_secret_env_as_name_only(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)
    profile = get_profile("sandbox.subfinder@v1")
    secret = "shodan-key-123"
    argv = runner.build_argv(
        profile,
        "run_1",
        "subfinder",
        ["-d", "example.com"],
        "http://samurai:tok@host.docker.internal:9",
        {"HOME": "/tmp"},
        {"SHODAN_API_KEY": secret},
    )
    assert argv[argv.index("SHODAN_API_KEY") - 1] == "--env"
    assert "SHODAN_API_KEY" in argv and f"SHODAN_API_KEY={secret}" not in argv
    assert secret not in " ".join(argv)
    # non-secret env keeps its value on the command line
    assert "HOME=/tmp" in argv


def test_runner_rejects_env_outside_profile_declaration(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)
    profile = get_profile("sandbox.subfinder@v1")
    with pytest.raises(ToolValidationError, match="not declared"):
        runner.build_argv(
            profile,
            "run_1",
            "subfinder",
            ["-d", "example.com"],
            "http://tok@host:9",
            None,
            {"EXFIL_API_KEY": "v"},
        )
    with pytest.raises(ToolValidationError, match="empty"):
        runner.build_argv(
            profile,
            "run_1",
            "subfinder",
            ["-d", "example.com"],
            "http://tok@host:9",
            None,
            {"SHODAN_API_KEY": ""},
        )
    with pytest.raises(ToolValidationError, match="single line"):
        runner.build_argv(
            profile,
            "run_1",
            "subfinder",
            ["-d", "example.com"],
            "http://tok@host:9",
            None,
            {"SHODAN_API_KEY": "a\nb"},
        )


def test_run_appends_proxy_flag_and_forwards_child_env(tmp_path: Path) -> None:
    runner, fake = make_runner(tmp_path)
    sign_default(tmp_path)
    profile = get_profile("sandbox.subfinder@v1")
    outcome = runner.run(
        profile,
        "run_1",
        "subfinder",
        ["-d", "example.com"],
        ScopeContract(includes=("example.com",)),
        env={"HOME": "/tmp"},
        secret_env={"SHODAN_API_KEY": "k"},
    )
    assert outcome.result.stdout == "hello"
    docker_run = fake.calls[-1]
    assert docker_run[docker_run.index("-proxy") - 1] == "example.com"
    assert docker_run[docker_run.index("-proxy") + 1].startswith("http://samurai:")
    assert fake.envs[-1] == {"HOME": "/tmp", "SHODAN_API_KEY": "k"}


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
    outcome = runner.run(profile, "run_xyz", "echo", ["hello sandbox"])
    result = outcome.result
    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert outcome.proxy_token is None
    assert outcome.egress_events == ()
    run_calls = [call for call in fake.calls if call[:2] == ("docker", "run")]
    assert len(run_calls) == 1
    assert run_calls[0][-4:] == ("--entrypoint", "echo", profile.image, "hello sandbox")


def test_run_enforces_profile_output_limit(tmp_path: Path) -> None:
    sign_default(tmp_path)
    manager = FakeManager(
        {("docker", "run"): ProcessResult(exit_code=0, stdout="x" * 100000, stderr="")}
    )
    runner, _ = make_runner(tmp_path, manager)
    profile = get_profile("sandbox.default@v1")
    result = runner.run(profile, "run_big", "cat", ["big.txt"]).result
    assert len(result.stdout.encode("utf-8")) <= profile.max_output_bytes
    assert result.truncated


def test_scoped_profile_requires_scope(tmp_path: Path) -> None:
    sign_default(tmp_path)
    runner, fake = make_runner(tmp_path)
    profile = get_profile("sandbox.scoped@v1")
    with pytest.raises(Exception, match="requires the operation scope"):
        runner.run(profile, "run_noscope", "echo", ["hi"])
    assert not [call for call in fake.calls if call[:2] == ("docker", "run")]


def test_scoped_profile_builds_proxy_argv(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)
    profile = get_profile("sandbox.scoped@v1")
    argv = runner.build_argv(
        profile, "run_s1", "wget", ["http://example.com/"],
        proxy_url="http://samurai:token123@host.docker.internal:8080",
    )
    assert "--network" not in argv
    assert argv[argv.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    env_values = [argv[i + 1] for i, part in enumerate(argv) if part == "--env"]
    assert "HTTP_PROXY=http://samurai:token123@host.docker.internal:8080" in env_values
    assert "HTTPS_PROXY=http://samurai:token123@host.docker.internal:8080" in env_values
    assert "NO_PROXY=" in env_values
    assert argv[-4:] == ["--entrypoint", "wget", profile.image, "http://example.com/"]


def test_scoped_profile_argv_requires_proxy_url(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)
    profile = get_profile("sandbox.scoped@v1")
    with pytest.raises(Exception, match="requires a scoped egress proxy"):
        runner.build_argv(profile, "run_s2", "wget", [])


def test_scoped_run_starts_and_stops_proxy(tmp_path: Path) -> None:
    sign_default(tmp_path)
    runner, fake = make_runner(tmp_path)
    profile = get_profile("sandbox.scoped@v1")
    scope = ScopeContract(includes=("127.0.0.1",))
    outcome = runner.run(profile, "run_scoped", "echo", ["hi"], scope)
    assert outcome.result.exit_code == 0
    assert outcome.proxy_token is not None
    assert outcome.egress_events == ()
    run_calls = [call for call in fake.calls if call[:2] == ("docker", "run")]
    argv = run_calls[0]
    env_values = [argv[i + 1] for i, part in enumerate(argv) if part == "--env"]
    proxy_env = [value for value in env_values if value.startswith("HTTP_PROXY=")]
    assert proxy_env
    assert outcome.proxy_token in proxy_env[0]


def test_sign_allowlist_writes_signature_file(tmp_path: Path) -> None:
    signature = sign_allowlist(tmp_path / "home" / "sandbox-allowlist.sig", MASTER_KEY)
    path = tmp_path / "home" / "sandbox-allowlist.sig"
    assert path.read_text(encoding="ascii").strip() == signature
    assert path.stat().st_mode & 0o777 == 0o600
