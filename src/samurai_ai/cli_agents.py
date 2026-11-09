from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from samurai_ai.agents.blackboard import Blackboard
from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.domain.agents import AgentRun
from samurai_ai.domain.execution import ApprovalRequiredError
from samurai_ai.domain.swarm import AgentSpecialization
from samurai_ai.services.models import ModelService
from samurai_ai.services.questions import (
    Answer,
    OperatorQuestionError,
    OperatorQuestionService,
)


def register_agent_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    agent = subcommands.add_parser("agent", help="Run and inspect governed agent runs")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)

    run = agent_commands.add_parser("run", help="Run the orchestrator on a plan task")
    run.add_argument("operation_id")
    run.add_argument("task_id")
    run.add_argument("--model", dest="model_id", default=None)
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
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        help="approve every tool call in place — for unattended runs",
    )
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
    run.add_argument(
        "--specialization",
        dest="specialization",
        default="general",
        choices=[item.value for item in AgentSpecialization],
        help="prompt profile for the run (strategist proposes hypotheses)",
    )

    list_command = agent_commands.add_parser("list", help="List agent runs")
    list_command.add_argument("operation_id")

    show = agent_commands.add_parser("show", help="Show an agent run with route and usage")
    show.add_argument("run_id")

    questions = agent_commands.add_parser(
        "questions", help="List operator questions a run left open"
    )
    questions.add_argument("operation_id")

    answer = agent_commands.add_parser(
        "answer", help="Answer a pending question call by its id"
    )
    answer.add_argument("call_id")
    answer.add_argument(
        "--select",
        action="append",
        default=[],
        metavar="ID=LABEL[,LABEL]",
        help="option label(s) to pick for one question; repeatable",
    )
    answer.add_argument(
        "--custom",
        action="append",
        default=[],
        metavar="ID=TEXT",
        help="free-form answer for one question; repeatable",
    )


def _always_approve(_approval_id: str) -> bool:
    """An unattended run has nobody to click; the operator said so up front."""
    return True


def _wall_deadline(settings: Any | None) -> float | None:
    """`agent.max_wall_seconds` from settings; blank or 0 disables the cap."""
    if settings is None:
        return None
    raw = (settings.get_value("agent.max_wall_seconds") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def handle_agent_command(
    args: argparse.Namespace,
    orchestrator: AgentOrchestrator,
    models: ModelService,
    settings: Any | None = None,
    questions: OperatorQuestionService | None = None,
) -> int | None:
    if args.command != "agent":
        return None
    if args.agent_command == "questions":
        rows = [] if questions is None else questions.pending(args.operation_id)
        if args.json_output:
            _print_json(
                [
                    {
                        "call_id": row.call_id,
                        "question": row.id.split(".", 1)[1],
                        "payload": json.loads(row.payload),
                        "asked_at": row.id,
                    }
                    for row in rows
                ]
            )
        elif not rows:
            print("no open questions")
        for row in rows:
            payload = json.loads(row.payload)
            question = next(
                item
                for item in payload["questions"]
                if item["id"] == row.id.split(".", 1)[1]
            )
            print(f"{row.call_id}\t{question['id']}\t{question['question']}")
            for option in question["options"]:
                mark = "*" if option.get("recommended") else " "
                print(f"  {mark} {option['label']}")
        return 0
    if args.agent_command == "answer":
        if questions is None:
            print("error: operator questions are not wired here", file=sys.stderr)
            return 2
        selections: dict[str, tuple[str, ...]] = {}
        for item in args.select:
            name, _, labels = item.partition("=")
            selections[name.strip()] = tuple(
                label.strip() for label in labels.split(",") if label.strip()
            )
        customs = {i.partition("=")[0].strip(): i.partition("=")[2] for i in args.custom}
        answers = [
            Answer(
                id=name,
                selected=selections.get(name, ()),
                custom=customs.get(name, ""),
            )
            for name in list(dict.fromkeys([*selections, *customs]))
        ]
        if not answers:
            print("error: pass --select or --custom", file=sys.stderr)
            return 2
        try:
            rows = questions.answer(args.call_id, answers, by="operator")
        except OperatorQuestionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"answered {len(rows)} question(s) of {args.call_id}")
        return 0
    if args.agent_command == "run":
        model_id = args.model_id or models.default_model()
        if model_id is None:
            print(
                "error: no model selected; pass --model or pick a default "
                "in the TUI via /model",
                file=sys.stderr,
            )
            return 2
        try:
            run = orchestrator.run_task(
                args.operation_id,
                args.task_id,
                [model_id, *args.fallbacks],
                max_steps=args.max_steps,
                max_wall_seconds=_wall_deadline(settings),
                approval_id=args.approval_id,
                workspace=args.workspace,
                skill_names=tuple(args.skills),
                approval_resolver=(
                    _always_approve if args.auto_approve else None
                ),
                specialization=AgentSpecialization(args.specialization),
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
