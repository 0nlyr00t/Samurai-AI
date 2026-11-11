from samurai_ai.domain.permission import (
    DEFAULT_WINDOW_SECONDS,
    LONG_WINDOW_SECONDS,
    QUICK_WINDOW_SECONDS,
    CommandRisk,
    PermissionMode,
    bound_network_command,
    classify_argv,
    classify_command,
    command_window,
    next_mode,
    parse_mode,
)


def test_mode_cycle_and_properties() -> None:
    assert next_mode(PermissionMode.ASK) is PermissionMode.YOLO
    assert next_mode(PermissionMode.YOLO) is PermissionMode.NEVER
    assert next_mode(PermissionMode.NEVER) is PermissionMode.ASK

    assert PermissionMode.ASK.prompts_for_tools is True
    assert PermissionMode.YOLO.prompts_for_tools is False
    assert PermissionMode.NEVER.prompts_for_tools is False
    # only "never" tells the agent to stop asking clarifying questions
    assert PermissionMode.YOLO.allows_operator_questions is True
    assert PermissionMode.NEVER.allows_operator_questions is False


def test_parse_mode_falls_back_for_unknown_values() -> None:
    assert parse_mode("yolo") is PermissionMode.YOLO
    assert parse_mode(" NEVER ") is PermissionMode.NEVER
    assert parse_mode("nonsense") is PermissionMode.ASK
    assert parse_mode(None) is PermissionMode.ASK
    assert parse_mode(None, PermissionMode.YOLO) is PermissionMode.YOLO


def test_sensitive_paths_are_flagged() -> None:
    for command in (
        "cat .env",
        "cat .env.local",
        "bat /Users/ops/.env",
        "ssh-add ~/.ssh/id_ed25519",
        "ls ~/.ssh/",
        "cat deploy/tls.pem",
        "aws configure --profile x && cat ~/.aws/credentials",
        "cat ~/.docker/config.json",
        "jq . ~/.kube/config",
        "cat /etc/shadow",
    ):
        risk = classify_command(command)
        assert risk.sensitive, command
        assert "credentials/key file" in risk.label()


def test_ordinary_commands_are_quiet() -> None:
    for command in (
        "git status",
        "nmap -sV 10.10.10.10",
        "curl -sI https://example.com",
        "cat environment.md",
        "grep -r password src",
        "python -m pytest -q",
    ):
        risk = classify_command(command)
        assert risk.empty, command
        assert risk.badges == ()


def test_destructive_shapes_are_flagged() -> None:
    cases = {
        "rm -rf build": "recursive delete",
        "rm -r data": "recursive delete",
        "dd if=/dev/zero of=/dev/sda": "raw disk write",
        "mkfs.ext4 /dev/sdb1": "filesystem create",
        "shutdown -h now": "host power",
        "git reset --hard origin/main": "git discard",
        "docker system prune -af": "container/volume delete",
        "iptables -F": "firewall flush",
        "psql -c 'DROP TABLE users'": "schema drop",
        "chmod -R 777 /opt": "recursive permission change",
    }
    for command, label in cases.items():
        assert label in classify_command(command).destructive, command


def test_write_outside_cwd_only_counts_with_a_cwd() -> None:
    command = "nmap -oX /tmp/scan.xml 10.10.10.10"
    assert "write outside cwd" in classify_command(command, cwd="/work/op").destructive
    assert "write outside cwd" not in classify_command(command, cwd="/tmp").destructive
    assert classify_command(command).destructive == ()


def test_badges_are_bounded_and_deduplicated() -> None:
    command = "rm -rf .env ~/.ssh id_rsa x.pem credentials.txt secrets.yaml"
    risk = classify_command(command)
    assert len(risk.destructive) <= 4
    assert len(risk.sensitive) <= 4
    assert risk.label().count("credentials/key file") == 1


def test_classify_argv_matches_command_classification() -> None:
    argv = ("bash", "-lc", "rm -rf .env")
    assert classify_argv(argv) == classify_command(" ".join(argv))
    assert isinstance(classify_argv(()), CommandRisk)


def test_the_command_family_sets_the_window() -> None:
    """A flat timeout killed every full-port scan and stalled every curl."""
    assert command_window("nmap -Pn -p- 10.0.0.5") == LONG_WINDOW_SECONDS
    assert command_window("cd /tmp && ffuf -u https://x/FUZZ") == LONG_WINDOW_SECONDS
    assert command_window("sudo nuclei -u https://x") == LONG_WINDOW_SECONDS
    curl = "curl -s -o /dev/null -w '%{http_code}' https://x"
    assert command_window(curl) == QUICK_WINDOW_SECONDS
    assert command_window("dig +short x.htb") == QUICK_WINDOW_SECONDS
    assert command_window("id; uname -a") == DEFAULT_WINDOW_SECONDS
    # the far side of a VPN answers on its own schedule
    assert command_window("nmap -p- 10.0.0.5", relayed=True) == LONG_WINDOW_SECONDS * 2


def test_probe_families_get_a_short_outer_window() -> None:
    for command in (
        "nxc rdp 10.0.0.5 -u test -p test",
        "sshpass -p test ssh user@10.0.0.5 id",
        "timeout 20 xfreerdp3 /v:10.0.0.5",
    ):
        assert command_window(command) == 60.0
        assert command_window(command, relayed=True) == 120.0


def test_nxc_and_ssh_get_transport_timeouts_without_overriding_explicit_ones() -> None:
    nxc = bound_network_command("nxc rdp 10.0.0.5 -u test -p test 2>&1 | tail -20")
    assert "--timeout 10" in nxc
    assert "--rdp-timeout 10" in nxc
    assert nxc.endswith("2>&1 | tail -20")

    already = bound_network_command("nxc rdp 10.0.0.5 --timeout 30 --rdp-timeout 40")
    assert already.split().count("--timeout") == 1
    assert "--rdp-timeout 40" in already

    ssh = bound_network_command("sshpass -p test ssh user@10.0.0.5 id")
    assert "-o ConnectTimeout=8" in ssh
    assert "-o ConnectionAttempts=1" in ssh

    explicit = bound_network_command("ssh -o ConnectTimeout=20 user@10.0.0.5 id")
    assert "ConnectTimeout=20" in explicit
    assert "ConnectTimeout=8" not in explicit


def test_transport_bounds_do_not_touch_other_commands_or_quoted_payloads() -> None:
    for command in (
        "nmap -Pn 10.0.0.5",
        "echo nxc rdp 10.0.0.5",
        "bash -c 'nxc rdp 10.0.0.5'",
        "echo ssh user@10.0.0.5",
    ):
        assert bound_network_command(command) == command
