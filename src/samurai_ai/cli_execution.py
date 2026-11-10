from __future__ import annotations

import argparse
import asyncio
import json
import shutil
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
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.permission import Decision
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.manifests import ManifestStore, ManifestStoreError


def register_execution_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    tool = subcommands.add_parser("tool", help="Manage and run governed tools")
    tool_commands = tool.add_subparsers(dest="tool_command", required=True)

    tool_commands.add_parser("list", help="List registered tool definitions")

    capabilities = tool_commands.add_parser(
        "capabilities",
        help="What this machine can run right now (read-only, changes nothing)",
    )
    capabilities.add_argument("--json", action="store_true", dest="json_output")

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

    provision = tool_commands.add_parser(
        "provision", help="Install a tool from the reviewed catalogue (dry-run first)"
    )
    provision.add_argument("tool_id")
    provision.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="show the plan, change nothing"
    )

    verify = tool_commands.add_parser("verify", help="Probe an installed tool, read-only")
    verify.add_argument("tool_id")

    tool_commands.add_parser("manifests", help="List installed manifests")

    uninstall = tool_commands.add_parser("uninstall", help="Remove an installed manifest")
    uninstall.add_argument("manifest_id")


def _rule_decision(
    args: argparse.Namespace, execution: ToolExecutionService
) -> tuple[Decision | None, dict[str, Any]]:
    """The operator's rule decision, asked at the same boundary the run uses."""
    params = parse_tool_params(args.params)
    definition = execution.registry.get(args.tool_id).definition
    return execution.rule_decision(definition, params), params


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
    args: argparse.Namespace, execution: ToolExecutionService
) -> int | None:
    if args.command != "tool":
        return None
    if args.tool_command == "capabilities":
        return _capabilities(args)
    if args.tool_command == "provision":
        return _provision(args, execution)
    if args.tool_command == "verify":
        return _verify(args)
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
        decision, params = _rule_decision(args, execution)
        approval_id = args.approval_id
        if decision is not None:
            _record_rule(execution, args, decision)
            if decision.is_denied:
                rule = decision.rule.format() if decision.rule is not None else "deny"
                print(f"denied by rule: {rule}", file=sys.stderr)
                return 1
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


def _capabilities(args: argparse.Namespace) -> int:
    """Read-only: detection never installs, pulls or asks for a password."""
    from samurai_ai.services.fabric import describe_capabilities

    summary = describe_capabilities()
    if getattr(args, "json_output", False):
        _print_json(summary.model_dump(mode="json"))
        return 0
    print(f"os\t{summary.os_family} {summary.distro} {summary.arch}".strip())
    for line in summary.tools:
        print(f"{line.tool_id}\t{line.path or '-'}\t{line.version or '-'}")
    if summary.notes:
        for note in summary.notes:
            print(f"note\t{note}")
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


def _provisioner(operation_service: Any) -> Any:
    """A provisioner over the real state directory; no operation needed for a plan."""
    from samurai_ai.config import config_directory
    from samurai_ai.services.catalog import portability_scripts
    from samurai_ai.services.provisioner import Provisioner

    return Provisioner(
        operations=operation_service,
        kernel=None,
        tools_dir=config_directory() / "tools",
        templates=portability_scripts(),
    )


def _profile(offline: bool) -> Any:
    from samurai_ai.domain.environment import EnvironmentDetector

    return EnvironmentDetector(offline=offline).detect(refresh=True)


def _provision(args: argparse.Namespace, execution: ToolExecutionService) -> int:
    """Dry-run by default is not enough: the operator passes --dry-run explicitly."""
    from samurai_ai.domain.provision import ProvisionError
    from samurai_ai.services.provisioner import Provisioner

    operations = execution.operations
    operation = operations.latest()
    provisioner = _provisioner(operations)
    try:
        profile = _profile(operation.offline if operation is not None else False)
        plan = provisioner.plan(args.tool_id, profile)
    except ProvisionError as exc:
        print(f"[provision] {exc}", file=sys.stderr)
        manual = getattr(exc, "manual", "")
        if manual:
            print(f"manual: {manual}", file=sys.stderr)
        return 2
    for line in Provisioner.describe(plan):
        print(line)
    if args.dry_run:
        print("dry-run: nothing was changed")
        return 0
    if operation is None:
        print(
            "no operation: create one first (approvals are recorded on an operation)",
            file=sys.stderr,
        )
        return 2
    approval = execution.kernel.request_approval(
        operation.id,
        f"provision {plan.tool_id} v{plan.recipe_version} via {plan.manager}",
        RiskClass.R3,
        reason="system package installation from the reviewed catalogue",
        requested_by="operator",
    )
    from samurai_ai.domain.kernel import ApprovalStatus

    if approval.status is not ApprovalStatus.APPROVED:
        execution.kernel.approve(approval.id, decided_by="operator")
    if provisioner.needs_decision(args.tool_id):
        print("[provision] the last attempt failed: this retries it on purpose", file=sys.stderr)
    try:
        run = provisioner.run(operation.id, args.tool_id, profile)
    except ProvisionError as exc:
        print(f"[provision] {exc}", file=sys.stderr)
        return 2
    if run.status.value != "ready":
        print(f"[provision] {run.error}", file=sys.stderr)
        return 1
    print(f"ready\t{run.tool_id}\t{provisioner.state(run.tool_id).installed_version}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    """Read-only: run the recipe healthcheck and say whether the tool answers."""
    from samurai_ai.services.catalog import RecipeCatalog

    recipe = RecipeCatalog().get(args.tool_id)
    if recipe is None:
        print(f"[verify] {args.tool_id} is not in the reviewed catalogue", file=sys.stderr)
        return 2
    path = shutil.which(recipe.executable)
    if path is None:
        print(f"[verify] {recipe.executable} is not on PATH", file=sys.stderr)
        return 1
    from samurai_ai.execution.process import ProcessManager

    argv = [path, *recipe.provision.healthcheck]
    result = asyncio.run(
        ProcessManager(allowed_executables=frozenset({recipe.executable})).run(
            argv, timeout_seconds=20.0, inherit_env=False
        )
    )
    from samurai_ai.services.provisioner import version_line

    detail = version_line(result.stdout or result.stderr) or "no output"
    print(f"{recipe.id}\t{path}\t{detail}")
    return 0 if result.exit_code == 0 else 1
