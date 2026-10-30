from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from samurai_ai.domain.execution import (
    ApprovalRequiredError,
    ToolResult,
    ToolRun,
    ToolValidationError,
)
from samurai_ai.services.execution import ToolExecutionService


def register_execution_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    tool = subcommands.add_parser("tool", help="Manage and run governed tools")
    tool_commands = tool.add_subparsers(dest="tool_command", required=True)

    tool_commands.add_parser("list", help="List registered tool definitions")

    run = tool_commands.add_parser("run", help="Run a tool under governance")
    run.add_argument("operation_id")
    run.add_argument("tool_id")
    run.add_argument(
        "--param",
        action="append",
        dest="params",
        default=[],
        metavar="KEY=VALUE",
        help="Tool parameter; VALUE is parsed as JSON when possible",
    )
    run.add_argument("--task", dest="task_id")
    run.add_argument("--approval-id", dest="approval_id")
    run.add_argument("--tool-version", dest="tool_version", type=int)

    show = tool_commands.add_parser("show", help="Show a tool run")
    show.add_argument("run_id")


def handle_execution_command(
    args: argparse.Namespace, execution: ToolExecutionService
) -> int | None:
    if args.command != "tool":
        return None
    if args.tool_command == "list":
        definitions = execution.list_tools()
        if args.json_output:
            _print_json([definition.model_dump(mode="json") for definition in definitions])
        else:
            for definition in definitions:
                network = f"network={definition.network.value}"
                print(
                    f"{definition.id}\tv{definition.version}\t{definition.kind.value}\t"
                    f"{definition.risk_class.value}\t{network}\t{definition.description}"
                )
        return 0
    if args.tool_command == "run":
        params = parse_tool_params(args.params)
        try:
            result = execution.run_tool(
                args.operation_id,
                args.tool_id,
                params,
                task_id=args.task_id,
                approval_id=args.approval_id,
                tool_version=args.tool_version,
            )
        except ApprovalRequiredError as exc:
            if args.json_output:
                print(
                    json.dumps(
                        {"error": "approval-required", "approval_id": exc.approval_id},
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
            else:
                print(
                    f"error: approval required; approve with "
                    f"'samurai approval approve {exc.approval_id}' and re-run with "
                    f"--approval-id {exc.approval_id}",
                    file=sys.stderr,
                )
            return 2
        emit_tool_result(result, args.json_output)
        return 0 if result.run.exit_code == 0 else 1
    run = execution.get_run(args.run_id)
    _emit_run(run, args.json_output)
    return 0


def parse_tool_params(pairs: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for pair in pairs:
        key, separator, raw_value = pair.partition("=")
        if not separator or not key:
            raise ToolValidationError(f"invalid --param {pair!r}; expected key=value")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        params[key] = value
    return params


def emit_tool_result(result: ToolResult, json_output: bool) -> None:
    if json_output:
        _print_json(result.model_dump(mode="json"))
        return
    _print_run_line(result.run)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")


def _emit_run(run: ToolRun, json_output: bool) -> None:
    if json_output:
        _print_json(run.model_dump(mode="json"))
        return
    _print_run_line(run)
    print(f"params: {json.dumps(run.parameters, ensure_ascii=False)}")
    print(f"approval: {run.approval_id or '-'}")
    print(f"recovery-action: {run.recovery_action_id or '-'}")
    print(f"stdout-artifact: {run.stdout_artifact_id or '-'}")
    print(f"stderr-artifact: {run.stderr_artifact_id or '-'}")
    print(f"redactions: {run.redaction_count}")
    print(f"warnings: {', '.join(run.injection_warnings) or '-'}")


def _print_run_line(run: ToolRun) -> None:
    print(
        f"{run.id}\t{run.tool_id}\tv{run.tool_version}\t{run.status.value}\t"
        f"exit={run.exit_code if run.exit_code is not None else '-'}\t"
        f"trust={run.trust.value}"
    )


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
