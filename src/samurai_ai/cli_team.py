"""`samurai team`: who is on an operation, and the notes they leave."""

from __future__ import annotations

import argparse
import sys

from samurai_ai.domain.access import AccessError, Principal
from samurai_ai.domain.teamwork import NoteTarget, OperationLevel, TeamworkError
from samurai_ai.services.operations import OperationService
from samurai_ai.services.teamwork import TeamworkService


def handle_team_command(
    args: argparse.Namespace,
    team: TeamworkService,
    operations: OperationService,
    actor: Principal,
) -> int | None:
    if args.command != "team":
        return None
    try:
        if args.team_command == "members":
            operations.get(args.operation_id)
            team.assert_visible(args.operation_id, actor)
            owner = team.owner(args.operation_id)
            print(f"owner: {owner or '-'}")
            for member in team.members(args.operation_id):
                print(f"{member.label or member.subject}\t{member.level.value}")
            return 0
        if args.team_command == "add":
            level = OperationLevel.VIEWER if args.viewer else OperationLevel.MEMBER
            member = team.add_member(args.operation_id, args.subject, level=level, actor=actor)
            print(f"{member.label} joined as {member.level.value}")
            return 0
        if args.team_command == "rm":
            print(
                f"{args.subject} removed"
                if team.remove_member(args.operation_id, args.subject, actor=actor)
                else f"{args.subject} was not on the operation"
            )
            return 0
        if args.team_command == "transfer":
            team.transfer(args.operation_id, args.subject, actor=actor)
            print(f"{args.subject} now owns the operation")
            return 0
        # note
        note = team.note(
            args.operation_id, NoteTarget(args.kind), args.target, args.text, actor=actor
        )
        print(f"note {note.id} on {note.target_kind.value} {note.target_id}")
        return 0
    except (TeamworkError, AccessError) as exc:
        print(f"[team] {exc}", file=sys.stderr)
        return 2
