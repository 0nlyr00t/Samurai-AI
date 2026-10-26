from __future__ import annotations

import argparse
import json
from pathlib import Path

from samurai_ai.services.operations import OperationService


def register_events_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    events = subcommands.add_parser(
        "events", help="Export the operation event journal"
    )
    events_commands = events.add_subparsers(dest="events_command", required=True)
    export = events_commands.add_parser(
        "export", help="Export the journal as NDJSON (one event per line)"
    )
    export.add_argument("operation_id")
    export.add_argument(
        "--out",
        type=Path,
        default=None,
        help="destination file; stdout when omitted",
    )


def handle_events_command(
    args: argparse.Namespace, operations: OperationService
) -> int | None:
    if args.command != "events":
        return None
    lines = [
        json.dumps(
            {
                "id": event.id,
                "created_at": event.created_at.isoformat(),
                "event_type": event.event_type,
                "payload": event.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for event in operations.events(args.operation_id)
    ]
    text = "\n".join(lines) + ("\n" if lines else "")
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
        print(f"exported {len(lines)} events to {args.out}")
    else:
        print(text, end="")
    return 0
