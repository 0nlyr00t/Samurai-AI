from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from samurai_ai.domain.kernel import ApprovalStatus
from samurai_ai.domain.swarm import AgentSpecialization, SwarmError, SwarmRun
from samurai_ai.services.models import ModelService
from samurai_ai.services.swarm import SwarmService, always_approve


def register_swarm_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    swarm = subcommands.add_parser(
        "swarm", help="Run and inspect parallel specialized agent swarms"
    )
    swarm_commands = swarm.add_subparsers(dest="swarm_command", required=True)

    ready = swarm_commands.add_parser("ready", help="List plan tasks ready for a swarm")
    ready.add_argument("operation_id")

    run = swarm_commands.add_parser("run", help="Run a swarm over independent plan tasks")
    run.add_argument("operation_id")
    run.add_argument(
        "--task",
        action="append",
        dest="task_ids",
        default=None,
        metavar="TASK_ID",
        help="Task to include; repeatable. Default: all ready tasks",
    )
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--model", dest="model_id", required=True)
    run.add_argument(
        "--fallback",
        action="append",
        dest="fallbacks",
        default=[],
        metavar="MODEL_ID",
    )
    run.add_argument(
        "--spec",
        action="append",
        dest="specs",
        default=[],
        metavar="TASK_ID:SPECIALIZATION",
        help=(
            "Member specialization (general|recon|testing|analysis|strategist|"
            "researcher); repeatable"
        ),
    )
    run.add_argument("--approval-id", dest="approval_id")
    run.add_argument(
        "--ask",
        dest="auto_approve",
        action="store_false",
        help="stop for an operator on every tool call instead of approving",
    )
    run.add_argument(
        "--workspace",
        dest="workspace",
        default=None,
        help="campaign memory workspace to inject into all members (read-only)",
    )
    run.add_argument(
        "--skill",
        action="append",
        dest="skills",
        default=[],
        metavar="SKILL_NAME",
        help="installed skill to inject into member prompts; repeatable",
    )

    list_command = swarm_commands.add_parser("list", help="List swarms of an operation")
    list_command.add_argument("operation_id")

    show = swarm_commands.add_parser(
        "show", help="Show a swarm report with wall time vs sequential estimate"
    )
    show.add_argument("swarm_id")


def handle_swarm_command(
    args: argparse.Namespace, swarms: SwarmService, models: ModelService
) -> int | None:
    if args.command != "swarm":
        return None
    if args.swarm_command == "ready":
        tasks = swarms.ready_tasks(args.operation_id)
        if args.json_output:
            _print_json([task.model_dump(mode="json") for task in tasks])
        else:
            for task in tasks:
                deps = ",".join(task.depends_on) or "-"
                print(f"{task.id}\t{task.title}\tdeps={deps}")
        return 0
    if args.swarm_command == "run":
        specializations = _parse_specs(args.specs)
        model_id = args.model_id or models.default_model()
        if model_id is None:
            print(
                "error: no model selected; pass --model or pick a default "
                "in the TUI via /model",
                file=sys.stderr,
            )
            return 2
        route = [model_id, *args.fallbacks]
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
            args.task_ids,
            route,
            workers=args.workers,
            specializations=specializations,
            approval_id=approval_id,
            approval_resolver=(always_approve if args.auto_approve else None),
            workspace=args.workspace,
            skill_names=tuple(args.skills),
        )
        if args.json_output:
            _print_json(swarms.report(swarm.id))
        else:
            _print_swarm(swarm)
            for member in swarms.members(swarm.id):
                print(
                    f"  {member.task_id}\t{member.id}\t{member.status.value}\t"
                    f"{member.specialization.value}"
                )
        return 0 if swarm.status.value == "completed" else 1
    if args.swarm_command == "list":
        swarm_list = swarms.list_swarms(args.operation_id)
        if args.json_output:
            _print_json([swarm.model_dump(mode="json") for swarm in swarm_list])
        else:
            for swarm in swarm_list:
                print(
                    f"{swarm.id}\t{swarm.status.value}\tworkers={swarm.workers}\t"
                    f"tasks={','.join(swarm.task_ids)}\t{swarm.created_at.isoformat()}"
                )
        return 0
    report = swarms.report(args.swarm_id)
    if args.json_output:
        _print_json(report)
    else:
        _print_report(report)
    return 0


def _parse_specs(raw_specs: list[str]) -> dict[str, AgentSpecialization]:
    specializations: dict[str, AgentSpecialization] = {}
    for raw in raw_specs:
        task_id, separator, value = raw.partition(":")
        if not separator or not task_id or not value:
            raise SwarmError(
                f"invalid --spec {raw!r}; expected TASK_ID:SPECIALIZATION"
            )
        try:
            specialization = AgentSpecialization(value)
        except ValueError:
            allowed = ", ".join(item.value for item in AgentSpecialization)
            raise SwarmError(
                f"unknown specialization {value!r}; allowed: {allowed}"
            ) from None
        if task_id in specializations:
            raise SwarmError(f"duplicate --spec for task {task_id!r}")
        specializations[task_id] = specialization
    return specializations


def _print_swarm(swarm: SwarmRun) -> None:
    print(
        f"{swarm.id}\t{swarm.status.value}\tworkers={swarm.workers}\t"
        f"tasks={','.join(swarm.task_ids)}"
    )


def _print_report(report: dict[str, Any]) -> None:
    print(
        f"{report['id']}\t{report['status']}\tworkers={report['workers']}\t"
        f"tasks={','.join(report['task_ids'])}"
    )
    for member in report["members"]:
        duration = member["duration_seconds"]
        duration_text = f"{duration}s" if duration is not None else "-"
        usage = member["usage"]
        print(
            f"  {member['task_id']}\t{member['run_id']}\t{member['status']}\t"
            f"{member['specialization']}\t{duration_text}\t"
            f"calls={usage['calls']} cost=${usage['cost_usd']}"
        )
    usage = report["usage"]
    print(
        f"usage: calls={usage['calls']} "
        f"tokens={usage['prompt_tokens']}+{usage['completion_tokens']} "
        f"cost=${usage['cost_usd']}"
    )
    if report["wall_time_seconds"] is not None:
        print(
            f"wall: {report['wall_time_seconds']}s vs sequential "
            f"~{report['sequential_estimate_seconds']}s (speedup {report['speedup']}x)"
        )


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
