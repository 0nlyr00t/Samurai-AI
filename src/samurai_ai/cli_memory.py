from __future__ import annotations

import argparse
import json

from samurai_ai.domain.memory import MemoryKind
from samurai_ai.services.memory import CampaignMemoryService


def register_memory_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    memory = subcommands.add_parser(
        "memory", help="Manage workspace-scoped campaign memory"
    )
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)

    add = memory_commands.add_parser("add", help="Add a campaign memory entry")
    add.add_argument("workspace")
    add.add_argument("title")
    add.add_argument("--body", default="")
    add.add_argument("--kind", choices=[kind.value for kind in MemoryKind], default="note")
    add.add_argument("--tags", default="", help="comma-separated tags")
    add.add_argument("--retention-days", dest="retention_days", type=int, default=None)

    list_command = memory_commands.add_parser("list", help="List memory entries")
    list_command.add_argument("--workspace", default=None)

    show = memory_commands.add_parser("show", help="Show a memory entry")
    show.add_argument("entry_id")

    update = memory_commands.add_parser("update", help="Update a memory entry")
    update.add_argument("entry_id")
    update.add_argument("--title", default=None)
    update.add_argument("--body", default=None)
    update.add_argument(
        "--kind", choices=[kind.value for kind in MemoryKind], default=None
    )
    update.add_argument("--tags", default=None, help="comma-separated tags")
    update.add_argument("--retention-days", dest="retention_days", type=int, default=None)
    update.add_argument(
        "--clear-retention", action="store_true", help="remove the retention limit"
    )

    delete = memory_commands.add_parser(
        "delete", help="Delete a memory entry (memory is not evidence)"
    )
    delete.add_argument("entry_id")

    memory_commands.add_parser("purge", help="Delete expired memory entries")

    memory_commands.add_parser("workspaces", help="List memory workspaces")


def handle_memory_command(
    args: argparse.Namespace, memory: CampaignMemoryService
) -> int | None:
    if args.command != "memory":
        return None
    if args.memory_command == "add":
        entry = memory.add(
            args.workspace,
            args.title,
            args.body,
            kind=MemoryKind(args.kind),
            tags=tuple(item.strip() for item in args.tags.split(",") if item.strip()),
            retention_days=args.retention_days,
        )
        if args.json_output:
            _print_json(entry.model_dump(mode="json"))
        else:
            print(f"{entry.id}\t{entry.workspace}\t{entry.kind.value}\t{entry.title}")
        return 0
    if args.memory_command == "list":
        entries = memory.list_entries(args.workspace)
        if args.json_output:
            _print_json([entry.model_dump(mode="json") for entry in entries])
        else:
            for entry in entries:
                retention = (
                    f"retention={entry.retention_days}d"
                    if entry.retention_days
                    else "retention=-"
                )
                print(
                    f"{entry.id}\t{entry.workspace}\t{entry.kind.value}\t"
                    f"{entry.title}\t{retention}"
                )
        return 0
    if args.memory_command == "show":
        entry = memory.get(args.entry_id)
        if args.json_output:
            _print_json(entry.model_dump(mode="json"))
        else:
            print(f"{entry.id}\t{entry.workspace}\t{entry.kind.value}\tv{entry.version}")
            print(f"title: {entry.title}")
            if entry.tags:
                print(f"tags: {', '.join(entry.tags)}")
            if entry.retention_days:
                print(f"retention: {entry.retention_days} days")
            if entry.body:
                print(entry.body)
        return 0
    if args.memory_command == "update":
        entry = memory.update(
            args.entry_id,
            title=args.title,
            body=args.body,
            kind=MemoryKind(args.kind) if args.kind else None,
            tags=(
                tuple(item.strip() for item in args.tags.split(",") if item.strip())
                if args.tags is not None
                else None
            ),
            retention_days=args.retention_days,
            clear_retention=args.clear_retention,
        )
        if args.json_output:
            _print_json(entry.model_dump(mode="json"))
        else:
            print(f"{entry.id}\tv{entry.version}\t{entry.title}")
        return 0
    if args.memory_command == "delete":
        memory.delete(args.entry_id)
        print(f"deleted {args.entry_id}")
        return 0
    if args.memory_command == "purge":
        purged = memory.purge_expired()
        if args.json_output:
            _print_json({"purged": purged})
        else:
            print(f"purged {len(purged)} expired entries")
            for entry_id in purged:
                print(f"  {entry_id}")
        return 0
    workspaces = memory.list_workspaces()
    if args.json_output:
        _print_json({"workspaces": workspaces})
    else:
        for workspace in workspaces:
            print(workspace)
    return 0


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
