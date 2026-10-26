from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from samurai_ai.agents.blackboard import Blackboard
from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.domain.agents import AgentRun
from samurai_ai.domain.execution import ApprovalRequiredError
from samurai_ai.services.models import ModelService


def register_agent_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    agent = subcommands.add_parser("agent", help="Run and inspect governed agent runs")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)

    run = agent_commands.add_parser("run", help="Run the orchestrator on a plan task")
    run.add_argument("operation_id")
    run.add_argument("task_id")
    run.add_argument("--model", dest="model_id", required=True)
    run.add_argument(
        "--fallback",
        action="append",
        dest="fallbacks",
        default=[],
        metavar="MODEL_ID",
    )
    run.add_argument("--max-steps", dest="max_steps", type=int, default=8)
    run.add_argument("--approval-id", dest="approval_id")
    run.add_argument(
        "--workspace",
        dest="workspace",
        default=None,
        help="campaign memory workspace to inject (read-only)",
    )
    run.add_argument(
        "--skill",
        action="append",
        dest="skills",
        default=[],
        metavar="SKILL_NAME",
        help="installed skill to inject into the executor prompt; repeatable",
    )

    list_command = agent_commands.add_parser("list", help="List agent runs")
    list_command.add_argument("operation_id")

    show = agent_commands.add_parser("show", help="Show an agent run with route and usage")
    show.add_argument("run_id")


def handle_agent_command(
    args: argparse.Namespace, orchestrator: AgentOrchestrator, models: ModelService
) -> int | None:
    if args.command != "agent":
        return None
    if args.agent_command == "run":
        try:
            run = orchestrator.run_task(
                args.operation_id,
                args.task_id,
                [args.model_id, *args.fallbacks],
                max_steps=args.max_steps,
                approval_id=args.approval_id,
                workspace=args.workspace,
                skill_names=tuple(args.skills),
            )
        except ApprovalRequiredError as exc:
            print(
                f"error: approval required; approve with "
                f"'samurai approval approve {exc.approval_id}' and re-run with "
                f"--approval-id {exc.approval_id}",
                file=sys.stderr,
            )
            return 2
        if args.json_output:
            _print_json(run.model_dump(mode="json"))
        else:
            print(
                f"{run.id}\t{run.task_id}\t{run.status.value}\tdepth={run.depth}\t"
                f"route={','.join(run.model_route)}"
            )
        return 0 if run.status.value == "completed" else 1
    if args.agent_command == "list":
        runs = orchestrator.list_runs(args.operation_id)
        if args.json_output:
            _print_json([run.model_dump(mode="json") for run in runs])
        else:
            for run in runs:
                print(
                    f"{run.id}\t{run.task_id}\t{run.status.value}\tdepth={run.depth}\t"
                    f"{run.created_at.isoformat()}"
                )
        return 0
    run = orchestrator.get_run(args.run_id)
    report = _run_report(orchestrator, models, run)
    if args.json_output:
        _print_json(report)
    else:
        _print_report(report)
    return 0


def _run_report(
    orchestrator: AgentOrchestrator, models: ModelService, run: AgentRun
) -> dict[str, Any]:
    events = orchestrator.operations.events(run.operation_id)
    steps: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    cost = 0.0
    calls = 0
    providers: set[str] = set()
    for event in events:
        payload = event.payload
        if event.event_type == "agent.step" and payload.get("run_id") == run.id:
            steps.append(payload)
        elif event.event_type == "agent.command.rejected" and payload.get("run_id") == run.id:
            rejected.append(payload)
        elif event.event_type == "model.call" and payload.get("agent_run_id") == run.id:
            calls += 1
            prompt_tokens += int(payload.get("prompt_tokens") or 0)
            completion_tokens += int(payload.get("completion_tokens") or 0)
            cost += float(payload.get("estimated_cost_usd") or 0.0)
            providers.add(str(payload.get("provider_id") or "unknown"))

    data_route = []
    for provider_id in sorted(providers):
        try:
            provider = models.registry.get_provider(provider_id)
        except Exception:
            continue
        data_route.append(
            {
                "provider": provider.id,
                "local": provider.local,
                "base_url": provider.base_url,
            }
        )

    context_limit = None
    try:
        model, _ = models.resolve_model(run.model_route[0])
        context_limit = model.context_limit
    except Exception:
        pass
    context_pct = (
        round(100.0 * prompt_tokens / context_limit, 1) if context_limit else None
    )

    blackboard = Blackboard.from_events(events)
    return {
        "id": run.id,
        "operation_id": run.operation_id,
        "task_id": run.task_id,
        "status": run.status.value,
        "depth": run.depth,
        "parent_run_id": run.parent_run_id,
        "model_route": list(run.model_route),
        "steps": steps,
        "rejected_commands": rejected,
        "usage": {
            "calls": calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": round(cost, 6),
        },
        "context": {"limit": context_limit, "used_pct": context_pct},
        "data_route": data_route,
        "blackboard_keys": blackboard.keys_for(run.id),
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def _print_report(report: dict[str, Any]) -> None:
    print(
        f"{report['id']}\t{report['task_id']}\t{report['status']}\t"
        f"depth={report['depth']}"
    )
    print(f"route: {' -> '.join(report['model_route'])}")
    print(
        f"usage: calls={report['usage']['calls']} "
        f"tokens={report['usage']['prompt_tokens']}+{report['usage']['completion_tokens']} "
        f"cost=${report['usage']['cost_usd']}"
    )
    context = report["context"]
    if context["limit"]:
        print(f"context: {context['used_pct']}% of {context['limit']} tokens")
    for route in report["data_route"]:
        local = "local" if route["local"] else "remote"
        print(f"data-route: {route['provider']} ({local}) {route['base_url']}")
    for step in report["steps"]:
        print(f"  step {step.get('step')}: {step.get('title', '-')}")
    for rejected in report["rejected_commands"]:
        print(f"  rejected: {rejected['command'].get('type')} ({rejected['reason']})")
    if report["blackboard_keys"]:
        print(f"blackboard: {', '.join(report['blackboard_keys'])}")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
