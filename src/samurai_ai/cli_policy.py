"""`samurai policy`: role floors for one operation (tighten only)."""

from __future__ import annotations

import argparse
import sys

from samurai_ai.domain.access import Principal, Role
from samurai_ai.domain.teamwork import OperationLevel
from samurai_ai.services.operations import OperationService
from samurai_ai.services.teamwork import TeamworkService
from samurai_ai.storage.access_policy import OperationPolicyRepository


def register_policy_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    policy = subcommands.add_parser(
        "policy", help="Role floors inside one operation (owner, tighten only)"
    )
    commands = policy.add_subparsers(dest="policy_command", required=True)
    listing = commands.add_parser("list", help="List operation role floors")
    listing.add_argument("operation_id")
    setter = commands.add_parser("set", help="Raise the role a command needs here")
    setter.add_argument("operation_id")
    setter.add_argument("path", help="command path, e.g. 'tool.run' or 'report'")
    setter.add_argument("role", choices=[role.value for role in Role])
    remover = commands.add_parser("rm", help="Drop an operation role floor")
    remover.add_argument("operation_id")
    remover.add_argument("path")


def handle_policy_command(
    args: argparse.Namespace,
    policies: OperationPolicyRepository,
    operations: OperationService,
    team: TeamworkService,
    actor: Principal,
) -> int | None:
    if args.command != "policy":
        return None
    operations.get(args.operation_id)
    team.assert_visible(args.operation_id, actor)
    if args.policy_command == "list":
        rows = policies.effective_paths(args.operation_id)
        if not rows:
            print("no floors")
            return 0
        for path, role in sorted(rows.items()):
            print(f"{path}\t{role.value}")
        return 0
    # only an owner may change the policy of their operation
    if team.level(args.operation_id, actor) is not OperationLevel.OWNER:
        if actor.role is not Role.ADMIN:
            print("error: only the operation owner can change its policy", file=sys.stderr)
            return 2
    if args.policy_command == "set":
        policies.set(args.operation_id, args.path, Role(args.role))
        print(f"set\t{args.path}\t{args.role}")
        return 0
    if policies.remove(args.operation_id, args.path):
        print(f"removed\t{args.path}")
        return 0
    print(f"no floor on {args.path}", file=sys.stderr)
    return 1


__all__ = ["handle_policy_command", "register_policy_commands"]
