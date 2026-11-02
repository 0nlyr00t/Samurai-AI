from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from samurai_ai.config import tools_directory
from samurai_ai.domain.execution import (
    ApprovalRequiredError,
    ToolResult,
    ToolRun,
    ToolValidationError,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.permission import Decision, classify_command, decide, parse_mode
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.manifests import ManifestStore, ManifestStoreError
from samurai_ai.services.settings import SettingsService


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

    install = tool_commands.add_parser(
        "install", help="Install a tool manifest (adds the tool to the registry)"
    )
    install.add_argument("path", type=Path)
    install.add_argument(
        "--trusted",
        action="store_true",
        help="required: you read the manifest and its image digest",
    )
    install.add_argument("--replace", action="store_true")

    tool_commands.add_parser("manifests", help="List installed manifests")

    uninstall = tool_commands.add_parser("uninstall", help="Remove an installed manifest")
    uninstall.add_argument("manifest_id")


def _rule_decision(
    args: argparse.Namespace, settings: SettingsService
) -> tuple[Decision | None, dict[str, Any]]:
    """The operator's rule decision for this call, if the tool takes a command."""
    params = parse_tool_params(args.params)
    command = params.get("command")
    if not isinstance(command, str):
        return None, params
    mode = parse_mode(settings.get_value("tui.permission_mode"))
    risk = classify_command(command)
    decision = decide(settings.permission_rules(), args.tool_id, command, mode, risk)
    return decision, params


def _record_rule(
    execution: ToolExecutionService,
    args: argparse.Namespace,
    decision: Decision | None,
) -> None:
    if decision is None or decision.rule is None:
        return
    execution.operations.repository.append_event(
        OperationEvent(
            operation_id=args.operation_id,
            event_type="permission.rule",
            payload={
                "rule": decision.rule.format(),
                "action": decision.action,
                "tool_id": args.tool_id,
            },
        )
    )


def handle_execution_command(
    args: argparse.Namespace,
    execution: ToolExecutionService,
    settings: SettingsService,
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
    if args.tool_command == "install":
        store = ManifestStore(tools_directory())
        try:
            manifest = store.install(
                args.path, trusted=args.trusted, replace=args.replace
            )
        except ManifestStoreError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"installed\t{manifest.id}\tv{manifest.version}\t{manifest.profile_id}")
        return 0
    if args.tool_command == "manifests":
        store = ManifestStore(tools_directory())
        manifests = store.load_all()
        if not manifests:
            print("no manifests installed")
            return 0
        for manifest in manifests:
            tampered = "tampered" if store.is_tampered(manifest) else "ok"
            print(f"{manifest.id}\tv{manifest.version}\t{manifest.parser}\t{tampered}")
        return 0
    if args.tool_command == "uninstall":
        if ManifestStore(tools_directory()).remove(args.manifest_id):
            print(f"removed\t{args.manifest_id}")
            return 0
        print(f"no such manifest: {args.manifest_id}", file=sys.stderr)
        return 1
    if args.tool_command == "run":
        decision, params = _rule_decision(args, settings)
        rule = decision.rule if decision is not None else None
        approval_id = args.approval_id
        if decision is not None and rule is not None:
            _record_rule(execution, args, decision)
            if decision.is_denied:
                print(f"denied by rule: {rule.format()}", file=sys.stderr)
                return 1
            if decision.is_allowed and approval_id is None:
                definition = execution.registry.get(args.tool_id).definition
                approval = execution.kernel.request_approval(
                    args.operation_id,
                    f"tool.run {definition.id}",
                    definition.risk_class,
                    task_id=args.task_id,
                    reason=f"allowed by rule {rule.format()}",
                    requested_by="operator",
                )
                execution.kernel.approve(
                    approval.id, decided_by=f"rule:{rule.format()}"
                )
                approval_id = approval.id
        try:
            result = execution.run_tool(
                args.operation_id,
                args.tool_id,
                params,
                task_id=args.task_id,
                approval_id=approval_id,
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
