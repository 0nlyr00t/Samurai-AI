from __future__ import annotations

import argparse

from samurai_ai.domain.goal import Goal, GoalState
from samurai_ai.services.goals import GoalNotFoundError, GoalService

_JSON = "--json"


def register_goal_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    goal = subcommands.add_parser("goal", help="Manage operation goals (goal mode)")
    commands = goal.add_subparsers(dest="goal_command", required=True)

    create = commands.add_parser("create", help="Create a goal")
    create.add_argument("operation_id")
    create.add_argument("objective")
    create.add_argument("--finish-line", dest="finish_line", default="")
    create.add_argument("--budget-steps", dest="budget_steps", type=int, default=None)
    create.add_argument("--budget-seconds", dest="budget_seconds", type=int, default=None)
    create.add_argument(
        "--next", dest="queue", action="store_true", help="queue behind the current goal"
    )
    create.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)

    status = commands.add_parser("status", help="Show the current goal and the queue")
    status.add_argument("operation_id")
    status.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)

    pause = commands.add_parser("pause", help="Pause the current goal")
    pause.add_argument("operation_id")
    pause.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)

    resume = commands.add_parser("resume", help="Resume the current goal")
    resume.add_argument("operation_id")
    resume.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)

    block = commands.add_parser("block", help="Block the current goal with a reason")
    block.add_argument("operation_id")
    block.add_argument("reason")
    block.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)

    complete = commands.add_parser("complete", help="Mark the current goal complete")
    complete.add_argument("operation_id")
    complete.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)

    cancel = commands.add_parser("cancel", help="Cancel the current goal")
    cancel.add_argument("operation_id")
    cancel.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)

    replace = commands.add_parser("replace", help="Cancel the current goal and start another")
    replace.add_argument("operation_id")
    replace.add_argument("objective")
    replace.add_argument("--finish-line", dest="finish_line", default="")
    replace.add_argument("--budget-steps", dest="budget_steps", type=int, default=None)
    replace.add_argument("--budget-seconds", dest="budget_seconds", type=int, default=None)
    replace.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)

    step = commands.add_parser("step", help="Count one step against the goal budget")
    step.add_argument("operation_id")
    step.add_argument(_JSON, dest="json_output", action="store_true", default=argparse.SUPPRESS)


def _emit(goal: Goal, json_output: bool) -> None:
    if json_output:
        print(goal.model_dump_json(indent=2))
        return
    print(
        f"{goal.id}\t{goal.state.value}\tsteps {goal.spent_steps}/{goal.budget_steps}\t"
        f"time {goal.spent_seconds}/{goal.budget_seconds}s"
    )
    print(f"  objective: {goal.objective}")
    if goal.finish_line:
        print(f"  finish line: {goal.finish_line}")
    if goal.block_reason:
        print(f"  blocked: {goal.block_reason}")


def handle_goal_command(args: argparse.Namespace, goals: GoalService) -> int | None:
    if args.command != "goal":
        return None
    operation_id = args.operation_id
    json_output = bool(getattr(args, "json_output", False))
    budgets: dict[str, int] = {}
    if getattr(args, "budget_steps", None) is not None:
        budgets["budget_steps"] = args.budget_steps
    if getattr(args, "budget_seconds", None) is not None:
        budgets["budget_seconds"] = args.budget_seconds

    match args.goal_command:
        case "create":
            _emit(
                goals.create(
                    operation_id,
                    args.objective,
                    finish_line=args.finish_line,
                    queue=args.queue,
                    **budgets,
                ),
                json_output,
            )
            return 0
        case "status":
            current = goals.current(operation_id)
            if current is None:
                print("no current goal")
            else:
                _emit(current, json_output)
            for queued in goals.list(operation_id):
                if queued.state is GoalState.QUEUED:
                    print(f"queued {queued.id}\t{queued.objective}")
            return 0
        case "pause":
            _emit(goals.pause(_current_id(goals, operation_id)), json_output)
            return 0
        case "resume":
            _emit(goals.resume(_current_id(goals, operation_id)), json_output)
            return 0
        case "block":
            _emit(goals.block(_current_id(goals, operation_id), args.reason), json_output)
            return 0
        case "complete":
            _emit(goals.complete(_current_id(goals, operation_id)), json_output)
            return 0
        case "cancel":
            _emit(goals.cancel(_current_id(goals, operation_id)), json_output)
            return 0
        case "replace":
            _emit(
                goals.replace(
                    operation_id,
                    args.objective,
                    finish_line=args.finish_line,
                    **budgets,
                ),
                json_output,
            )
            return 0
        case "step":
            _emit(goals.record_step(_current_id(goals, operation_id)), json_output)
            return 0
    return None


def _current_id(goals: GoalService, operation_id: str) -> str:
    current = goals.current(operation_id)
    if current is None:
        raise GoalNotFoundError(f"operation {operation_id} has no current goal")
    return current.id
