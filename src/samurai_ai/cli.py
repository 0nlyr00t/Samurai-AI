import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.cli_agents import handle_agent_command, register_agent_commands
from samurai_ai.cli_ctf import handle_ctf_command, register_ctf_commands
from samurai_ai.cli_execution import handle_execution_command, register_execution_commands
from samurai_ai.cli_kernel import handle_kernel_command, register_kernel_commands
from samurai_ai.cli_mcp import handle_mcp_command, register_mcp_commands
from samurai_ai.cli_models import handle_model_command, register_model_commands
from samurai_ai.cli_playbooks import handle_playbook_command, register_playbook_commands
from samurai_ai.cli_recovery import handle_recovery_command, register_recovery_commands
from samurai_ai.cli_reports import handle_report_command, register_report_commands
from samurai_ai.cli_sandbox import handle_sandbox_command, register_sandbox_commands
from samurai_ai.cli_vault import handle_vault_command, register_vault_commands
from samurai_ai.config import default_database_path, vault_key_path
from samurai_ai.domain.agents import AgentError, AgentRunNotFoundError
from samurai_ai.domain.ctf import (
    ChallengeNotFoundError,
    CtfError,
    HypothesisNotFoundError,
    WriteupNotFoundError,
)
from samurai_ai.domain.execution import ToolExecutionError, ToolNotFoundError
from samurai_ai.domain.kernel import KernelError
from samurai_ai.domain.mcp import McpServerNotFoundError
from samurai_ai.domain.models import ModelError, ModelNotFoundError
from samurai_ai.domain.operations import (
    Operation,
    OperationMode,
    OperationStatus,
    OperationTransitionError,
)
from samurai_ai.domain.playbooks import PlaybookError
from samurai_ai.domain.recovery import RecoveryStateError
from samurai_ai.domain.reports import ReportError, ReportNotFoundError
from samurai_ai.domain.scope import ScopeContract, ScopeValidationError
from samurai_ai.domain.vault import VaultError
from samurai_ai.execution.registry import default_registry
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.execution import ToolExecutionService, ToolRunNotFoundError
from samurai_ai.services.kernel import ApprovalNotFoundError, KernelService
from samurai_ai.services.mcp import McpService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationNotFoundError, OperationService
from samurai_ai.services.playbooks import PlaybookService, ProgramRulesNotFoundError
from samurai_ai.services.recovery import (
    PlanNotFoundError,
    RecoveryActionNotFoundError,
    RecoveryService,
    SnapshotNotFoundError,
)
from samurai_ai.services.reports import ReportService
from samurai_ai.services.settings import InvalidLocaleError, SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.ctf import CtfConflictError, CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunConflictError, ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.mcp import McpServerConflictError, McpServerRepository
from samurai_ai.storage.models import ModelEndpointRepository
from samurai_ai.storage.operations import OperationConflictError, OperationRepository
from samurai_ai.storage.playbooks import PlaybookConflictError, PlaybookRepository
from samurai_ai.storage.recovery import RecoveryConflictError, RecoveryRepository
from samurai_ai.storage.reports import ReportRepository
from samurai_ai.storage.vault import VaultRepository
from samurai_ai.tui.app import SamuraiApp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="samurai")
    parser.add_argument("--db", type=Path, help="Path to the Samurai SQLite database")
    parser.add_argument("--json", action="store_true", dest="json_output")
    subcommands = parser.add_subparsers(dest="command")

    operation = subcommands.add_parser("operation", help="Manage operations")
    operation_commands = operation.add_subparsers(dest="operation_command", required=True)

    create = operation_commands.add_parser("create", help="Create an operation")
    create.add_argument("name")
    create.add_argument("--mode", choices=[mode.value for mode in OperationMode], required=True)
    create.add_argument("--objective", default="")
    create.add_argument("--include", action="append", dest="includes", required=True)
    create.add_argument("--exclude", action="append", dest="excludes", default=[])

    operation_commands.add_parser("list", help="List operations")

    show = operation_commands.add_parser("show", help="Show an operation")
    show.add_argument("operation_id")
    show.add_argument("--events", action="store_true")

    transition = operation_commands.add_parser("transition", help="Change operation status")
    transition.add_argument("operation_id")
    transition.add_argument("status", choices=[status.value for status in OperationStatus])

    fork = operation_commands.add_parser(
        "fork", help="Fork an operation into a fresh copy without progress"
    )
    fork.add_argument("operation_id")
    fork.add_argument("--name", default=None)

    scope = subcommands.add_parser("scope", help="Evaluate operation scope")
    scope_commands = scope.add_subparsers(dest="scope_command", required=True)
    check = scope_commands.add_parser("check", help="Check a target against operation scope")
    check.add_argument("operation_id")
    check.add_argument("target")

    tui = subcommands.add_parser("tui", help="Open the terminal interface")
    tui.add_argument("--operation", dest="operation_id")

    register_kernel_commands(subcommands)
    register_recovery_commands(subcommands)
    register_execution_commands(subcommands)
    register_vault_commands(subcommands)
    register_sandbox_commands(subcommands)
    register_mcp_commands(subcommands)
    register_model_commands(subcommands)
    register_agent_commands(subcommands)
    register_playbook_commands(subcommands)
    register_report_commands(subcommands)
    register_ctf_commands(subcommands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    (
        operation_service,
        kernel_service,
        settings_service,
        recovery_service,
        execution_service,
        vault_service,
        mcp_service,
        model_service,
        agent_orchestrator,
        playbook_service,
        report_service,
        ctf_service,
    ) = _make_services(args.db)

    try:
        recovery_result = handle_recovery_command(
            args,
            recovery_service,
            settings_service,
            execution_service,
        )
        if recovery_result is not None:
            return recovery_result
        kernel_result = handle_kernel_command(args, kernel_service)
        if kernel_result is not None:
            return kernel_result
        execution_result = handle_execution_command(args, execution_service)
        if execution_result is not None:
            return execution_result
        vault_result = handle_vault_command(args, vault_service)
        if vault_result is not None:
            return vault_result
        sandbox_result = handle_sandbox_command(args)
        if sandbox_result is not None:
            return sandbox_result
        mcp_result = handle_mcp_command(
            args, mcp_service, operation_service, execution_service
        )
        if mcp_result is not None:
            return mcp_result
        model_result = handle_model_command(args, model_service)
        if model_result is not None:
            return model_result
        agent_result = handle_agent_command(args, agent_orchestrator, model_service)
        if agent_result is not None:
            return agent_result
        playbook_result = handle_playbook_command(args, playbook_service)
        if playbook_result is not None:
            return playbook_result
        report_result = handle_report_command(args, report_service)
        if report_result is not None:
            return report_result
        ctf_result = handle_ctf_command(args, ctf_service)
        if ctf_result is not None:
            return ctf_result
        if args.command == "operation":
            return _handle_operation(args, operation_service, kernel_service)
        if args.command == "scope":
            decision = operation_service.check_scope(args.operation_id, args.target)
            if args.json_output:
                _print_json(decision.model_dump(mode="json"))
            else:
                action = "ALLOW" if decision.allowed else "DENY"
                print(f"{action} {decision.target}: {decision.detail}")
            return 0 if decision.allowed else 1

        SamuraiApp(
            operation_service,
            kernel_service,
            settings_service,
            recovery_service,
            execution_service,
            vault_service,
            agent_orchestrator.runs,
            playbook_service,
            report_service,
            ctf_service,
            getattr(args, "operation_id", None),
        ).run()
        return 0
    except (
        OperationNotFoundError,
        OperationTransitionError,
        OperationConflictError,
        ScopeValidationError,
        KernelError,
        RecoveryStateError,
        RecoveryConflictError,
        SnapshotNotFoundError,
        RecoveryActionNotFoundError,
        PlanNotFoundError,
        ApprovalNotFoundError,
        InvalidLocaleError,
        ToolExecutionError,
        ToolNotFoundError,
        ToolRunNotFoundError,
        ToolRunConflictError,
        VaultError,
        McpServerNotFoundError,
        McpServerConflictError,
        ModelError,
        ModelNotFoundError,
        AgentError,
        AgentRunNotFoundError,
        PlaybookError,
        ProgramRulesNotFoundError,
        PlaybookConflictError,
        ReportError,
        ReportNotFoundError,
        CtfError,
        ChallengeNotFoundError,
        HypothesisNotFoundError,
        WriteupNotFoundError,
        CtfConflictError,
        ValidationError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _handle_operation(
    args: argparse.Namespace,
    service: OperationService,
    kernel: KernelService,
) -> int:
    if args.operation_command == "create":
        scope = ScopeContract(includes=tuple(args.includes), excludes=tuple(args.excludes))
        operation = service.create(args.name, OperationMode(args.mode), scope, args.objective)
        _emit_operation(operation, args.json_output)
        return 0

    if args.operation_command == "list":
        operations = service.list_all()
        if args.json_output:
            _print_json([operation.model_dump(mode="json") for operation in operations])
        else:
            for operation in operations:
                print(
                    f"{operation.id}\t{operation.mode.value}\t{operation.status.value}\t"
                    f"{operation.name}"
                )
        return 0

    if args.operation_command == "show":
        operation = service.get(args.operation_id)
        if args.json_output:
            payload = operation.model_dump(mode="json")
            if args.events:
                payload["events"] = [
                    event.model_dump(mode="json") for event in service.events(operation.id)
                ]
            _print_json(payload)
        else:
            _print_operation(operation)
            if args.events:
                for event in service.events(operation.id):
                    print(
                        f"{event.id}\t{event.created_at.isoformat()}\t{event.event_type}\t"
                        f"{json.dumps(event.payload, ensure_ascii=False)}"
                    )
        return 0

    operation = kernel.transition_operation(
        args.operation_id,
        OperationStatus(args.status),
    )
    _emit_operation(operation, args.json_output)
    return 0


def _make_services(
    database_path: Path | None,
) -> tuple[
    OperationService,
    KernelService,
    SettingsService,
    RecoveryService,
    ToolExecutionService,
    VaultService,
    McpService,
    ModelService,
    AgentOrchestrator,
    PlaybookService,
    ReportService,
    CtfService,
]:
    path = database_path or default_database_path()
    database = Database(path)
    operation_repository = OperationRepository(database)
    operations = OperationService(operation_repository)
    kernel = KernelService(operations, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    settings = SettingsService(recovery_repository)
    recovery = RecoveryService(operations, kernel, recovery_repository)
    vault = VaultService(VaultRepository(database), operation_repository, vault_key_path())
    mcp = McpService(McpServerRepository(database))
    model_endpoints = ModelEndpointRepository(database)
    models = ModelService(
        ModelRegistry(model_endpoints),
        vault,
        kernel,
        operations,
        model_endpoints,
        recovery_repository,
    )
    execution = ToolExecutionService(
        operations,
        kernel,
        recovery,
        ToolRunRepository(database),
        registry=default_registry(mcp.resolver()),
        vault=vault,
    )
    agents = AgentOrchestrator(
        models, kernel, execution, operations, AgentRunRepository(database)
    )
    playbooks = PlaybookService(operations, kernel, PlaybookRepository(database))
    reports = ReportService(operations, kernel, playbooks, ReportRepository(database), vault)
    ctf = CtfService(operations, kernel, CtfRepository(database), vault)
    return (
        operations,
        kernel,
        settings,
        recovery,
        execution,
        vault,
        mcp,
        models,
        agents,
        playbooks,
        reports,
        ctf,
    )


def _emit_operation(operation: Operation, json_output: bool) -> None:
    if json_output:
        _print_json(operation.model_dump(mode="json"))
    else:
        _print_operation(operation)


def _print_operation(operation: Operation) -> None:
    print(f"{operation.id}\t{operation.mode.value}\t{operation.status.value}\t{operation.name}")
    if operation.objective:
        print(f"objective: {operation.objective}")
    print(f"scope: {', '.join(operation.scope.includes)}")
    if operation.scope.excludes:
        print(f"excludes: {', '.join(operation.scope.excludes)}")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run() -> None:
    raise SystemExit(main())
