"""Headless mode: one-shot non-interactive batch execution for CI/CD.

`headless run` auto-selects all ready plan tasks, runs them as a swarm and
prints a JSON summary on stdout. Exit codes follow the stable contract:
0 completed, 1 partial/failed, 2 usage/approval errors.
"""

from __future__ import annotations

import argparse
import json
import sys

from samurai_ai.domain.kernel import ApprovalStatus
from samurai_ai.services.swarm import SwarmService


def register_headless_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    headless = subcommands.add_parser(
        "headless", help="Non-interactive batch execution for CI/CD pipelines"
    )
    headless_commands = headless.add_subparsers(dest="headless_command", required=True)
    run = headless_commands.add_parser(
        "run", help="Run all ready plan tasks as a swarm and print a JSON summary"
    )
    run.add_argument("operation_id")
    run.add_argument("--model", dest="model_id", required=True)
    run.add_argument(
        "--fallback", action="append", dest="fallbacks", default=[], metavar="MODEL_ID"
    )
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--approval-id", dest="approval_id")
    run.add_argument("--workspace", dest="workspace", default=None)
    run.add_argument(
        "--skill", action="append", dest="skills", default=[], metavar="SKILL_NAME"
    )


def handle_headless_command(args: argparse.Namespace, swarms: SwarmService) -> int | None:
    if args.command != "headless":
        return None
    route = [args.model_id, *args.fallbacks]
    approval_id = args.approval_id
    if approval_id is None:
        approval = swarms.prepare_model_approval(args.operation_id, route)
        if approval.status is not ApprovalStatus.APPROVED:
            print(
                f"error: approval required; approve with "
                f"'samurai approval approve {approval.id}' and re-run with "
                f"--approval-id {approval.id}",
                file=sys.stderr,
            )
            return 2
        approval_id = approval.id
    swarm = swarms.start(
        args.operation_id,
        None,
        route,
        workers=args.workers,
        approval_id=approval_id,
        workspace=args.workspace,
        skill_names=tuple(args.skills),
    )
    report = swarms.report(swarm.id)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if swarm.status.value == "completed" else 1
