from samurai_ai.domain.permission import (
    CommandRisk,
    PermissionMode,
    classify_argv,
    classify_command,
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
