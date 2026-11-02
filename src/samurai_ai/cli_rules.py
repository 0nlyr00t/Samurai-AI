"""`samurai rules`: operator-defined allow/ask/deny rules over command shapes."""

from __future__ import annotations

import argparse
import sys

from samurai_ai.domain.permission import PermissionRuleError
from samurai_ai.services.settings import SettingsService, TooManyRulesError


def register_rules_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    rules = subcommands.add_parser(
        "rules", help="Inspect and edit operator permission rules"
    )
    rules_commands = rules.add_subparsers(dest="rules_command", required=True)
    rules_commands.add_parser("list", help="List rules in matching order")
    add = rules_commands.add_parser("add", help="Add a rule, e.g. 'allow shell.exec: git *'")
    add.add_argument("rule")
    remove = rules_commands.add_parser("rm", help="Remove a rule")
    remove.add_argument("rule")


def handle_rules_command(
    args: argparse.Namespace, settings: SettingsService
) -> int | None:
    if args.command != "rules":
        return None
    try:
        if args.rules_command == "list":
            rules = settings.permission_rules()
            if not rules:
                print("no rules")
                return 0
            for rule in rules:
                print(rule.format())
            return 0
        if args.rules_command == "add":
            rule = settings.add_permission_rule(args.rule)
            print(f"added\t{rule.format()}")
            return 0
        if settings.remove_permission_rule(args.rule):
            print(f"removed\t{args.rule.strip()}")
            return 0
        print(f"no such rule: {args.rule.strip()}", file=sys.stderr)
        return 1
    except (PermissionRuleError, TooManyRulesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
