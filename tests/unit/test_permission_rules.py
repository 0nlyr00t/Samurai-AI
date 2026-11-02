"""Operator rules: parsing, matching order, and the old behaviour without them."""

from pathlib import Path

import pytest

from samurai_ai.domain.permission import (
    MAX_RULE_PATTERN,
    CommandRisk,
    PermissionMode,
    PermissionRule,
    PermissionRuleError,
    decide,
)


def rule(text: str) -> PermissionRule:
    return PermissionRule.parse(text)


def test_a_rule_round_trips_through_its_text_form() -> None:
    parsed = rule("allow shell.exec: git *")
    assert parsed.format() == "allow shell.exec: git *"
    assert parsed.action == "allow" and parsed.tool_id == "shell.exec"


def test_a_wildcard_is_a_text_glob_not_a_prefix() -> None:
    allow = rule("allow shell.exec: git *")
    assert allow.matches("shell.exec", "git push --force")
    assert not allow.matches("shell.exec", "gitk")
    assert not allow.matches("process.exec", "git push")


def test_a_tool_wildcard_covers_every_tool() -> None:
    wide = rule("deny *: rm -rf *")
    assert wide.matches("shell.exec", "rm -rf /tmp/x")
    assert wide.matches("process.exec", "rm -rf /tmp/x")


def test_an_exact_pattern_needs_the_whole_command() -> None:
    exact = rule("allow shell.exec: id")
    assert exact.matches("shell.exec", "id")
    assert not exact.matches("shell.exec", "id -u")


@pytest.mark.parametrize(
    "text",
    ["allow shell.exec", "shell.exec: git *", "permit shell.exec: git *", "allow shell.exec: "],
)
def test_broken_rule_text_is_rejected(text: str) -> None:
    with pytest.raises(PermissionRuleError):
        rule(text)


def test_an_overlong_pattern_is_rejected() -> None:
    with pytest.raises(PermissionRuleError, match="longer than"):
        rule("allow shell.exec: " + "a" * (MAX_RULE_PATTERN + 1))


def test_without_rules_the_old_behaviour_holds() -> None:
    clean = CommandRisk()
    risky = CommandRisk(destructive=("recursive delete",))
    def action(command: str, mode: PermissionMode, risk: CommandRisk, **kwargs: object):
        return decide((), "shell.exec", command, mode, risk, **kwargs).action

    assert action("ls", PermissionMode.ASK, clean) == "ask"
    assert action("ls", PermissionMode.ASK, clean, reusable=True) == "silent"
    assert action("rm -rf x", PermissionMode.ASK, risky, reusable=True) == "ask"
    assert action("rm -rf x", PermissionMode.YOLO, risky) == "silent"


def test_precedence_is_deny_then_ask_then_allow() -> None:
    rules = (
        rule("allow shell.exec: *"),
        rule("ask shell.exec: git *"),
        rule("deny shell.exec: *rm -rf*"),
    )
    clean = CommandRisk()

    def action(command: str) -> str:
        return decide(rules, "shell.exec", command, PermissionMode.YOLO, clean).action

    assert action("rm -rf /tmp/x") == "deny"
    assert action("git push") == "ask"
    assert action("ls") == "allow"


def test_an_allow_rule_outranks_the_badge_but_ask_rules_do_not() -> None:
    risky = CommandRisk(sensitive=("credentials/key file",))
    allowed = (rule("allow shell.exec: cat *"),)
    decision = decide(allowed, "shell.exec", "cat .env", PermissionMode.ASK, risky)
    assert decision.action == "allow"
    asked = (rule("ask shell.exec: cat *"),)
    assert decide(asked, "shell.exec", "cat .env", PermissionMode.YOLO, risky).action == "ask"


def test_the_deciding_rule_is_reported_for_the_journal() -> None:
    rule_obj = rule("deny shell.exec: rm -rf *")
    decision = decide((rule_obj,), "shell.exec", "rm -rf /", PermissionMode.YOLO, CommandRisk())
    assert decision.is_denied
    assert decision.rule is rule_obj


def test_settings_round_trip_and_broken_lines_are_skipped(tmp_path: Path) -> None:
    from samurai_ai.services.settings import SettingsService
    from samurai_ai.storage.database import Database
    from samurai_ai.storage.recovery import RecoveryRepository

    settings = SettingsService(RecoveryRepository(Database(tmp_path / "s.db")))
    settings.add_permission_rule("allow shell.exec: git *")
    assert [r.format() for r in settings.permission_rules()] == ["allow shell.exec: git *"]
    # a duplicate is a no-op, a removing call reports what it did
    settings.add_permission_rule("allow shell.exec: git *")
    assert len(settings.permission_rules()) == 1
    assert settings.remove_permission_rule("allow shell.exec: git *") is True
    assert settings.remove_permission_rule("allow shell.exec: git *") is False
    assert settings.permission_rules() == ()


def test_too_many_rules_are_refused(tmp_path: Path) -> None:
    from samurai_ai.domain.permission import MAX_RULES
    from samurai_ai.services.settings import SettingsService, TooManyRulesError
    from samurai_ai.storage.database import Database
    from samurai_ai.storage.recovery import RecoveryRepository

    settings = SettingsService(RecoveryRepository(Database(tmp_path / "m.db")))
    for index in range(MAX_RULES):
        settings.add_permission_rule(f"allow shell.exec: cmd{index}")
    with pytest.raises(TooManyRulesError):
        settings.add_permission_rule("allow shell.exec: onemore")


def test_a_broken_stored_line_does_not_take_the_others_down(tmp_path: Path) -> None:
    import json

    from samurai_ai.services.settings import SettingsService
    from samurai_ai.storage.database import Database
    from samurai_ai.storage.recovery import RecoveryRepository

    settings = SettingsService(RecoveryRepository(Database(tmp_path / "b.db")))
    settings.set_value(
        "permission.rules", json.dumps(["allow shell.exec: git *", "nonsense", 42])
    )
    assert [r.format() for r in settings.permission_rules()] == ["allow shell.exec: git *"]
