"""Headless mode: one-shot non-interactive batch execution for CI/CD.

`headless run` auto-selects all ready plan tasks, runs them as a swarm and
prints a JSON summary on stdout. Exit codes follow the stable contract:
0 completed, 1 partial/failed, 2 usage/approval errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from samurai_ai.domain.kernel import ApprovalStatus
from samurai_ai.services.swarm import SwarmService, always_approve


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
    run.add_argument(
        "--relay",
        default="",
        metavar="PREFIX",
        help="run network tools through this prefix (persisted on the operation)",
    )
    run.add_argument(
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=True,
        help="approve every tool call in place (default)",
    )
    run.add_argument(
        "--ask",
        dest="auto_approve",
        action="store_false",
        help="stop for an operator on every tool call",
    )
    run.add_argument("--workspace", dest="workspace", default=None)
    run.add_argument(
        "--skill", action="append", dest="skills", default=[], metavar="SKILL_NAME"
    )


def handle_headless_command(args: argparse.Namespace, swarms: SwarmService) -> int | None:
    if args.command != "headless":
        return None
    if args.relay:
        swarms.operations.set_relay(args.operation_id, args.relay)
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
    retried: set[str] = set()
    reports: list[dict[str, Any]] = []
    done = 0
    failed = 0
    while True:
        ready = swarms.ready_tasks(args.operation_id)
        if not ready:
            # a blocked task earns exactly one fresh attempt; then the plan
            # is honestly over
            plan = swarms.kernel.get_current_plan(args.operation_id)
            retryable = [
                task
                for task in (plan.tasks if plan else [])
                if task.status.value in ("blocked", "in-progress")
                and task.id not in retried
            ]
            if not retryable:
                break
            for task in retryable:
                retried.add(task.id)
                swarms.kernel.reset_task(args.operation_id, task.id)
            continue
        swarm = swarms.start(
            args.operation_id,
            [task.id for task in ready],
            route,
            workers=args.workers,
            approval_id=approval_id,
            workspace=args.workspace,
            skill_names=tuple(args.skills),
            approval_resolver=(always_approve if args.auto_approve else None),
        )
        report = swarms.report(swarm.id)
        reports.append(report)
        for member in report.get("members", []):
            if member.get("status") == "completed":
                done += 1
            else:
                failed += 1
    # one JSON document: the batch list plus the totals the caller reads
    last = dict(reports[-1]) if reports else {}
    last["batches"] = len(reports)
    last["tasks_done"] = done
    last["tasks_failed"] = failed
    # stdout stays a single JSON document; the totals ride inside it
    print(json.dumps(last, ensure_ascii=False, indent=2))
    if not reports:
        # nothing was ever ready and nothing could be retried: a usage error
        print("error: no ready tasks in the current plan", file=sys.stderr)
        return 2
    return 0 if failed == 0 and done else 1
