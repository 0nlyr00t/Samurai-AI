import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from samurai_ai import __version__
from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.cli_agents import handle_agent_command, register_agent_commands
from samurai_ai.cli_auth import handle_auth_command, handle_role_command
from samurai_ai.cli_ctf import handle_ctf_command, register_ctf_commands
from samurai_ai.cli_discover import handle_discover_command, register_discover_command
from samurai_ai.cli_doctor import register_doctor_command
from samurai_ai.cli_eval import handle_eval_command, register_eval_commands
from samurai_ai.cli_events import handle_events_command, register_events_commands
from samurai_ai.cli_execution import handle_execution_command, register_execution_commands
from samurai_ai.cli_genes import handle_gene_command, register_gene_commands
from samurai_ai.cli_goals import handle_goal_command, register_goal_commands
from samurai_ai.cli_headless import handle_headless_command, register_headless_commands
from samurai_ai.cli_kernel import handle_kernel_command, register_kernel_commands
from samurai_ai.cli_mcp import handle_mcp_command, register_mcp_commands
from samurai_ai.cli_memory import handle_memory_command, register_memory_commands
from samurai_ai.cli_models import handle_model_command, register_model_commands
from samurai_ai.cli_offline import handle_offline_command, register_offline_commands
from samurai_ai.cli_planning import handle_planning_command, register_planning_commands
from samurai_ai.cli_playbooks import handle_playbook_command, register_playbook_commands
from samurai_ai.cli_plugins import handle_plugin_command, register_plugin_commands
from samurai_ai.cli_policy import handle_policy_command, register_policy_commands
from samurai_ai.cli_recovery import handle_recovery_command, register_recovery_commands
from samurai_ai.cli_redteam import handle_redteam_command, register_redteam_commands
from samurai_ai.cli_reports import handle_report_command, register_report_commands
from samurai_ai.cli_rules import handle_rules_command, register_rules_commands
from samurai_ai.cli_run import add_run_command
from samurai_ai.cli_sandbox import handle_sandbox_command, register_sandbox_commands
from samurai_ai.cli_serve import add_serve_commands
from samurai_ai.cli_skills import handle_skill_command, register_skill_commands
from samurai_ai.cli_swarm import handle_swarm_command, register_swarm_commands
from samurai_ai.cli_team import handle_team_command
from samurai_ai.cli_vault import handle_vault_command, register_vault_commands
from samurai_ai.cli_web import handle_web_command, register_web_commands
from samurai_ai.config import default_database_path, genes_path, vault_key_path
from samurai_ai.domain.access import AccessError, Principal, Role
from samurai_ai.domain.agents import AgentError, AgentRunNotFoundError
from samurai_ai.domain.ctf import (
    ChallengeNotFoundError,
    CtfError,
    HypothesisNotFoundError,
    WriteupNotFoundError,
)
from samurai_ai.domain.eval import EvalError
from samurai_ai.domain.execution import ToolExecutionError, ToolNotFoundError
from samurai_ai.domain.goal import GoalError
from samurai_ai.domain.kernel import KernelError
from samurai_ai.domain.mcp import McpServerNotFoundError
from samurai_ai.domain.memory import MemoryError, MemoryNotFoundError
from samurai_ai.domain.models import ModelError, ModelNotFoundError
from samurai_ai.domain.operations import (
    Operation,
    OperationMode,
    OperationStatus,
    OperationTransitionError,
)
from samurai_ai.domain.planning import (
    PlannedTestNotFoundError,
    PlanningError,
    RunbookNotFoundError,
    ScenarioNotFoundError,
)
from samurai_ai.domain.playbooks import PlaybookError
from samurai_ai.domain.recovery import RecoveryStateError
from samurai_ai.domain.redteam import (
    AttackStepNotFoundError,
    CheckpointNotFoundError,
    DebriefNotFoundError,
    RedTeamError,
    RoeNotFoundError,
)
from samurai_ai.domain.reports import ReportError, ReportNotFoundError
from samurai_ai.domain.scope import ScopeContract, ScopeValidationError
from samurai_ai.domain.skills import SkillError, SkillNotFoundError
from samurai_ai.domain.swarm import SwarmError, SwarmNotFoundError
from samurai_ai.domain.teamwork import NoteTarget
from samurai_ai.domain.vault import VaultError
from samurai_ai.execution.registry import default_registry
from samurai_ai.self_update import run_update
from samurai_ai.services.access import AccessService
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.execution import ToolExecutionService, ToolRunNotFoundError
from samurai_ai.services.exports import IntegrationExportService
from samurai_ai.services.genes import GeneService, GeneStore
from samurai_ai.services.goals import GoalNotFoundError, GoalService
from samurai_ai.services.impact import ImpactService
from samurai_ai.services.kernel import ApprovalNotFoundError, KernelService
from samurai_ai.services.mcp import McpService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationNotFoundError, OperationService
from samurai_ai.services.planning import PlanningService
from samurai_ai.services.playbooks import PlaybookService, ProgramRulesNotFoundError
from samurai_ai.services.questions import OperatorQuestionService
from samurai_ai.services.recovery import (
    PlanNotFoundError,
    RecoveryActionNotFoundError,
    RecoveryService,
    SnapshotNotFoundError,
)
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.reports import ReportService
from samurai_ai.services.settings import InvalidLocaleError, SettingsService
from samurai_ai.services.skills import SkillService
from samurai_ai.services.swarm import SwarmService
from samurai_ai.services.teamwork import TeamworkService
from samurai_ai.services.traffic import TrafficService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.access import AccessRepository
from samurai_ai.storage.access_policy import OperationPolicyRepository
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.ctf import CtfConflictError, CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunConflictError, ToolRunRepository
from samurai_ai.storage.goals import GoalConflictError, GoalRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.mcp import McpServerConflictError, McpServerRepository
from samurai_ai.storage.memory import MemoryConflictError, MemoryRepository
from samurai_ai.storage.models import (
    CustomModelConflictError,
    CustomModelRepository,
    ModelEndpointConflictError,
    ModelEndpointRepository,
)
from samurai_ai.storage.operations import OperationConflictError, OperationRepository
from samurai_ai.storage.planning import PlanningConflictError, PlanningRepository
from samurai_ai.storage.playbooks import PlaybookConflictError, PlaybookRepository
from samurai_ai.storage.questions import QuestionRepository
from samurai_ai.storage.recovery import RecoveryConflictError, RecoveryRepository
from samurai_ai.storage.redteam import RedTeamConflictError, RedTeamRepository
from samurai_ai.storage.reports import ReportRepository
from samurai_ai.storage.skill_gaps import SkillGapRepository
from samurai_ai.storage.skills import SkillRepository
from samurai_ai.storage.swarm import SwarmRunRepository
from samurai_ai.storage.teamwork import TeamworkRepository
from samurai_ai.storage.traffic import TrafficRepository
from samurai_ai.storage.vault import VaultRepository
from samurai_ai.tui.app import SamuraiApp


class _VersionAction(argparse.Action):
    """``--version`` prints the mark next to the name, not just a number."""

    def __init__(self, option_strings: Sequence[str], **kwargs: object) -> None:
        super().__init__(option_strings, nargs=0, **kwargs)  # type: ignore[arg-type]

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        print_version_banner()
        parser.exit()


def print_version_banner() -> None:
    from rich.console import Console

    from samurai_ai.tui.theme import BUILTIN, DEFAULT_THEME

    Console().print(f"[bold {BUILTIN[DEFAULT_THEME]['accent']}]samurai[/] {__version__}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="samurai")
    parser.add_argument("--version", action=_VersionAction)
    parser.add_argument("--db", type=Path, help="Path to the Samurai SQLite database")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--socket", default=None, help="session daemon socket (serve/attach/send/call)"
    )
    subcommands = parser.add_subparsers(dest="command")

    update = subcommands.add_parser(
        "update", help="Update Samurai AI to the latest release"
    )
    update.add_argument(
        "--check",
        action="store_true",
        help="Only check whether a newer release exists",
    )

    operation = subcommands.add_parser("operation", help="Manage operations")
    operation_commands = operation.add_subparsers(dest="operation_command", required=True)

    create = operation_commands.add_parser("create", help="Create an operation")
    create.add_argument("name")
    create.add_argument("--mode", choices=[mode.value for mode in OperationMode], required=True)
    create.add_argument("--objective", default="")
    create.add_argument("--include", action="append", dest="includes", required=True)
    create.add_argument("--exclude", action="append", dest="excludes", default=[])
    create.add_argument(
        "--offline",
        action="store_true",
        help="the harness has no internet: local models only, no outside tools",
    )

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

    op_export = operation_commands.add_parser(
        "export", help="Export a read-only HTML dossier of the operation"
    )
    op_export.add_argument("operation_id")
    op_export.add_argument("--format", dest="format", choices=["html"], default="html")
    op_export.add_argument("--out", type=Path, required=True)

    team = subcommands.add_parser("team", help="Members and notes of an operation")
    team_commands = team.add_subparsers(dest="team_command", required=True)
    team_members = team_commands.add_parser("members", help="List the people on an operation")
    team_members.add_argument("operation_id")
    team_add = team_commands.add_parser("add", help="Add a signed-in person")
    team_add.add_argument("operation_id")
    team_add.add_argument("subject")
    team_add.add_argument("--viewer", action="store_true", help="read-only membership")
    team_rm = team_commands.add_parser("rm", help="Remove a member")
    team_rm.add_argument("operation_id")
    team_rm.add_argument("subject")
    team_transfer = team_commands.add_parser("transfer", help="Hand the operation over")
    team_transfer.add_argument("operation_id")
    team_transfer.add_argument("subject")
    team_note = team_commands.add_parser("note", help="Leave a note on a finding or a task")
    team_note.add_argument("operation_id")
    team_note.add_argument("kind", choices=[kind.value for kind in NoteTarget])
    team_note.add_argument("target")
    team_note.add_argument("text")

    scope = subcommands.add_parser("scope", help="Evaluate operation scope")
    scope_commands = scope.add_subparsers(dest="scope_command", required=True)
    host = scope_commands.add_parser(
        "host",
        help="Map a name to an address for this operation (vhosts, internal DNS)",
    )
    host_commands = host.add_subparsers(dest="scope_host_command", required=True)
    host_add = host_commands.add_parser("add", help="Add or replace a name mapping")
    host_add.add_argument("operation_id")
    host_add.add_argument("name", help="the name a target answers to, e.g. flight.htb")
    host_add.add_argument("target", help="address it means, in scope")
    host_rm = host_commands.add_parser("rm", help="Drop a name mapping")
    host_rm.add_argument("operation_id")
    host_rm.add_argument("name")
    host_list = host_commands.add_parser("list", help="List name mappings")
    host_list.add_argument("operation_id")

    scope_add = scope_commands.add_parser("add", help="Add an inclusion rule")
    scope_add.add_argument("operation_id")
    scope_add.add_argument("targets", nargs="+")
    scope_add.add_argument(
        "--yes", "-y", action="store_true", help="Do not ask before widening the scope"
    )
    scope_rm = scope_commands.add_parser("rm", help="Remove an inclusion rule")
    scope_rm.add_argument("operation_id")
    scope_rm.add_argument("targets", nargs="+")

    check = scope_commands.add_parser("check", help="Check a target against operation scope")
    check.add_argument("operation_id")
    check.add_argument("target")

    auth = subcommands.add_parser("auth", help="Sign in through the identity provider")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    configure = auth_commands.add_parser("configure", help="Set the provider issuer and client")
    configure.add_argument("issuer")
    configure.add_argument("client_id")
    configure.add_argument("--claim", default="", help="claim holding groups (default: groups)")
    auth_commands.add_parser("login", help="Start a device login")
    auth_commands.add_parser("whoami", help="Show the current identity and role")
    auth_commands.add_parser("logout", help="End the session")

    role = subcommands.add_parser("role", help="Map a provider claim to a role")
    role_commands = role.add_subparsers(dest="role_command", required=True)
    role_map = role_commands.add_parser("map", help="Map a claim value")
    role_map.add_argument("claim_value")
    role_map.add_argument("role", choices=[role.value for role in Role])
    role_rm = role_commands.add_parser("rm", help="Drop a mapping")
    role_rm.add_argument("claim_value")
    role_commands.add_parser("list", help="List mappings")

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
    register_redteam_commands(subcommands)
    register_planning_commands(subcommands)
    register_swarm_commands(subcommands)
    register_goal_commands(subcommands)
    register_memory_commands(subcommands)
    register_skill_commands(subcommands)
    register_events_commands(subcommands)
    register_headless_commands(subcommands)
    register_eval_commands(subcommands)
    register_rules_commands(subcommands)
    register_gene_commands(subcommands)
    register_policy_commands(subcommands)
    register_discover_command(subcommands)
    register_doctor_command(subcommands)
    register_plugin_commands(subcommands)
    register_web_commands(subcommands)
    register_offline_commands(subcommands)
    add_serve_commands(subcommands, subcommands.add_parser)
    add_run_command(subcommands, subcommands.add_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    global _ACCESS
    args = build_parser().parse_args(argv)
    if args.command == "update":
        # Self-update needs no database or services.
        return run_update(check=args.check)
    if args.command in {"serve", "attach", "send", "call", "run", "doctor"}:
        return int(args.handler(args) or 0)
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
        redteam_service,
        planning_service,
        swarm_service,
        goal_service,
        memory_service,
        skill_service,
        export_service,
        access_service,
        team_service,
        question_service,
    ) = _make_services(args.db)
    global _SERVICES
    _SERVICES = (
        operation_service, kernel_service, settings_service, recovery_service,
        execution_service, vault_service, mcp_service, model_service,
        agent_orchestrator, playbook_service, report_service, ctf_service,
        redteam_service, planning_service, swarm_service, goal_service,
        memory_service, skill_service, export_service, access_service,
        team_service, question_service,
    )
    operation_policies = OperationPolicyRepository(
        Database(args.db or default_database_path())
    )

    _ACCESS = access_service
    if args.command not in _OPEN_COMMANDS:
        try:
            _path = _command_path(args)
            access_service.require(
                _path,
                access_service.operation_role(
                    _operation_id_of(args, kernel_service), _path
                ),
            )
        except AccessError as exc:
            print(f"[access] {exc}", file=sys.stderr)
            return 2

    try:
        auth_result = handle_auth_command(args, access_service)
        if auth_result is not None:
            return auth_result
        role_result = handle_role_command(args, access_service)
        if role_result is not None:
            return role_result
        try:
            team_result = handle_team_command(
                args, team_service, operation_service, access_service.current()
            )
        except AccessError as exc:
            print(f"[access] {exc}", file=sys.stderr)
            return 2
        if team_result is not None:
            return team_result
        rules_result = handle_rules_command(args, settings_service)
        if rules_result is not None:
            return rules_result
        gene_result = handle_gene_command(args)
        if gene_result is not None:
            return gene_result
        discover_result = handle_discover_command(args, execution_service, operation_service)
        if discover_result is not None:
            return discover_result
        offline_result = handle_offline_command(args, _SERVICES)
        if offline_result is not None:
            return offline_result
        web_result = handle_web_command(args, _SERVICES)
        if web_result is not None:
            return web_result
        plugin_result = handle_plugin_command(args, _SERVICES)
        if plugin_result is not None:
            return plugin_result
        try:
            policy_result = handle_policy_command(
                args, operation_policies, operation_service, team_service,
                access_service.current(),
            )
        except AccessError as exc:
            print(f"[access] {exc}", file=sys.stderr)
            return 2
        if policy_result is not None:
            return policy_result
        recovery_result = handle_recovery_command(
            args,
            recovery_service,
            settings_service,
            execution_service,
        )
        if recovery_result is not None:
            return recovery_result
        impact_service = ImpactService(operation_service, kernel_service)
        kernel_result = handle_kernel_command(args, kernel_service, impact_service)
        if kernel_result is not None:
            return kernel_result
        execution_result = handle_execution_command(args, execution_service, settings_service)
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
        agent_result = handle_agent_command(
            args, agent_orchestrator, model_service, settings_service, question_service
        )
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
        redteam_result = handle_redteam_command(args, redteam_service, export_service)
        if redteam_result is not None:
            return redteam_result
        planning_result = handle_planning_command(args, planning_service, export_service)
        if planning_result is not None:
            return planning_result
        swarm_result = handle_swarm_command(args, swarm_service, model_service)
        if swarm_result is not None:
            return swarm_result
        goal_result = handle_goal_command(args, goal_service)
        if goal_result is not None:
            return goal_result
        memory_result = handle_memory_command(args, memory_service)
        if memory_result is not None:
            return memory_result
        skill_result = handle_skill_command(args, skill_service)
        if skill_result is not None:
            return skill_result
        events_result = handle_events_command(args, operation_service)
        if events_result is not None:
            return events_result
        headless_result = handle_headless_command(args, swarm_service)
        if headless_result is not None:
            return headless_result
        eval_result = handle_eval_command(args)
        if eval_result is not None:
            return eval_result
        if args.command == "operation":
            return _handle_operation(
                args, operation_service, kernel_service, export_service, team_service
            )
        if args.command == "scope" and args.scope_command in {"add", "rm"}:
            code = _handle_scope_rules(args, operation_service)
            if code is not None:
                return code
        if args.command == "scope" and args.scope_command == "host":
            code = _handle_scope_host(args, operation_service, args.json_output)
            if code is not None:
                return code
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
            agent_orchestrator,
            playbook_service,
            report_service,
            ctf_service,
            redteam_service,
            planning_service,
            swarm_service.swarms,
            goal_service,
            memory_service,
            skill_service,
            model_service,
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
        GoalError,
        GoalConflictError,
        GoalNotFoundError,
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
        RedTeamError,
        RoeNotFoundError,
        AttackStepNotFoundError,
        CheckpointNotFoundError,
        DebriefNotFoundError,
        RedTeamConflictError,
        PlanningError,
        ScenarioNotFoundError,
        PlannedTestNotFoundError,
        RunbookNotFoundError,
        PlanningConflictError,
        SwarmError,
        SwarmNotFoundError,
        MemoryError,
        MemoryNotFoundError,
        MemoryConflictError,
        ModelEndpointConflictError,
        CustomModelConflictError,
        SkillError,
        SkillNotFoundError,
        EvalError,
        ValidationError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _current_principal() -> Principal | None:
    """The signed-in person, or None when the store cannot answer."""
    if _ACCESS is None:  # pragma: no cover - only outside `main`
        return None
    try:
        return _ACCESS.current()
    except Exception:  # noqa: BLE001 - listing must not fail on identity
        return None


def _handle_operation(
    args: argparse.Namespace,
    service: OperationService,
    kernel: KernelService,
    exports: IntegrationExportService,
    team: TeamworkService,
) -> int:
    actor = _current_principal()

    if args.operation_command == "create":
        scope = ScopeContract(includes=tuple(args.includes), excludes=tuple(args.excludes))
        operation = service.create(
            args.name,
            OperationMode(args.mode),
            scope,
            objective=args.objective,
            offline=args.offline,
        )
        if actor is not None:
            team.claim_owner(operation.id, actor)
        _emit_operation(operation, args.json_output)
        return 0

    if args.operation_command == "list":
        operations = service.list_all()
        if actor is not None:
            operations = team.visible(operations, actor)
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

    if args.operation_command == "export":
        result = exports.export_dossier(args.operation_id, args.out)
        if args.json_output:
            _print_json(result)
        else:
            for file in result["files"]:
                print(f"{file['path']}\tsha256={file['sha256'][:16]}…")
        return 0

    operation = kernel.transition_operation(
        args.operation_id,
        OperationStatus(args.status),
    )
    _emit_operation(operation, args.json_output)
    return 0


#: Commands that must work before anyone can sign in.
_OPEN_COMMANDS = frozenset({"update", "auth", "role", "discover"})
#: Set once in `main` so handlers outside the service wiring can reach identity.
_ACCESS: AccessService | None = None


_OPERATION_ID_FIELDS = ("finding_id", "finding_ids", "artifact_id")


def _operation_id_of(args: argparse.Namespace, kernel: KernelService) -> str | None:
    """The operation a command belongs to, from args or from the id it addresses.

    Commands that name a finding or an artifact carry no operation, so the owner's
    policy would be bypassed by `finding show <id>`. An id that resolves to
    nothing (a typo) yields no operation: the global floor applies and the handler
    reports the id as unknown, which reads better than a denial.
    """
    direct = getattr(args, "operation_id", None)
    if direct:
        return str(direct)
    for field in _OPERATION_ID_FIELDS:
        value = getattr(args, field, None)
        if not value:
            continue
        for item in value if isinstance(value, list) else [value]:
            if field == "artifact_id":
                artifact = kernel.kernel.get_artifact(str(item))
                if artifact is not None:
                    return str(artifact.operation_id)
                continue
            finding = kernel.kernel.get_finding(str(item))
            if finding is not None:
                return str(finding.operation_id)
    return None


def _confirm_scope_add(scope: Any, target: str, args: argparse.Namespace) -> bool:
    """Ask before widening the scope; without a terminal, widen and say so.

    Narrowing is free and repeating a rule is free. A widening is the moment the
    operator stops knowing what the operation may touch, and a silent widening is
    how a /32 authorization quietly becomes a /16.
    """
    if getattr(args, "yes", False) or not sys.stdin.isatty():
        print(f"widened: {target}", file=sys.stderr)
        return True
    try:
        answer = input(f"scope widens: {target} — add it? [Y/n] ").strip().lower()
    except EOFError:
        return True
    if answer in ("", "y", "yes", "д", "да"):
        return True
    print(f"not added: {target}", file=sys.stderr)
    return False


def _handle_scope_rules(args: argparse.Namespace, operations: Any) -> int | None:
    """Bare `scope add/rm`: the shortest path between "I have a target" and work."""
    operation = operations.get(args.operation_id)
    includes = list(operation.scope.includes)
    for target in args.targets:
        if args.scope_command == "add":
            if target in includes:
                print(f"already in scope: {target}", file=sys.stderr)
                continue
            if not operation.scope.covers(target) and not _confirm_scope_add(
                operation.scope, target, args
            ):
                print(f"skipped: {target}", file=sys.stderr)
                continue
            includes.append(target)
        elif target not in includes:
            print(f"not in scope: {target}", file=sys.stderr)
            return 1
        else:
            includes.remove(target)
    try:
        scope = ScopeContract(
            includes=tuple(includes),
            excludes=operation.scope.excludes,
            hosts=operation.scope.hosts,
        )
    except ScopeValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    updated = operations.replace_scope(operation.id, scope)
    print(f"scope: {' '.join(updated.scope.includes) or '(empty)'}")
    return 0


def _handle_scope_host(
    args: argparse.Namespace, operations: Any, json_output: bool
) -> int | None:
    operation = operations.get(args.operation_id)
    hosts = dict(operation.scope.hosts)
    if args.scope_host_command == "list":
        if json_output:
            _print_json(hosts)
        elif not hosts:
            print("no host mappings")
        for name, target in sorted(hosts.items()):
            print(f"{name}\t{target}")
        return 0
    if args.scope_host_command == "rm":
        if hosts.pop(args.name, None) is None:
            print(f"no mapping for {args.name}", file=sys.stderr)
            return 1
    else:
        hosts[args.name] = args.target
    try:
        scope = ScopeContract(
            includes=operation.scope.includes,
            excludes=operation.scope.excludes,
            hosts=hosts,
        )
    except ScopeValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    updated = operations.replace_scope(operation.id, scope)
    if args.scope_host_command == "add":
        decision = updated.scope.evaluate(args.name)
        if not decision.allowed:
            print(
                f"warning: {args.name} maps to {args.target}, which is out of "
                f"scope ({decision.detail})",
                file=sys.stderr,
            )
        print(f"{args.name}\t{args.target}")
    else:
        print(f"removed\t{args.name}")
    return 0


_SERVICES: tuple[Any, ...] = ()


def _command_path(args: argparse.Namespace) -> str:
    action = getattr(args, f"{args.command}_command", "")
    return f"{args.command}.{action}" if action else str(args.command)


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
    RedTeamService,
    PlanningService,
    SwarmService,
    GoalService,
    CampaignMemoryService,
    SkillService,
    IntegrationExportService,
    AccessService,
    TeamworkService,
    OperatorQuestionService,
]:
    database = Database(database_path or default_database_path())
    operation_repository = OperationRepository(database)
    operations = OperationService(operation_repository)
    kernel = KernelService(operations, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    settings = SettingsService(recovery_repository)
    recovery = RecoveryService(operations, kernel, recovery_repository)
    vault = VaultService(VaultRepository(database), operation_repository, vault_key_path())
    mcp = McpService(McpServerRepository(database))
    model_endpoints = ModelEndpointRepository(database)
    custom_models = CustomModelRepository(database)
    models = ModelService(
        ModelRegistry(model_endpoints, custom_models=custom_models),
        vault,
        kernel,
        operations,
        model_endpoints,
        recovery_repository,
        custom_models=custom_models,
    )
    traffic_service = TrafficService(operations, kernel, TrafficRepository(database))
    execution = ToolExecutionService(
        operations,
        kernel,
        recovery,
        ToolRunRepository(database),
        registry=default_registry(mcp.resolver()),
        vault=vault,
        traffic=traffic_service,
        permission_rules=settings.permission_rules,
        permission_mode=lambda: settings.get_value("tui.permission_mode"),
    )
    memory_service = CampaignMemoryService(MemoryRepository(database), vault)
    skill_service = SkillService(
        SkillRepository(database), vault, SkillGapRepository(database)
    )
    questions = OperatorQuestionService(operations, QuestionRepository(database))
    agents = AgentOrchestrator(
        models,
        kernel,
        execution,
        operations,
        AgentRunRepository(database),
        memory=memory_service,
        skills=skill_service,
        genes=GeneService(GeneStore(genes_path())),
        impact=ImpactService(operations, kernel),
        questions=questions,
    )
    playbooks = PlaybookService(operations, kernel, PlaybookRepository(database))
    reports = ReportService(
        operations, kernel, playbooks, ReportRepository(database), vault,
        impact=ImpactService(operations, kernel),
    )
    ctf = CtfService(operations, kernel, CtfRepository(database), vault)
    redteam = RedTeamService(operations, kernel, RedTeamRepository(database), vault)
    planning = PlanningService(operations, kernel, PlanningRepository(database), vault)
    swarm = SwarmService(operations, kernel, agents, SwarmRunRepository(database))
    goals = GoalService(operations, GoalRepository(database))
    exports = IntegrationExportService(operations, kernel, redteam, planning, vault)
    access = AccessService(
        settings,
        vault,
        AccessRepository(database),
        policies=OperationPolicyRepository(database),
    )
    team = TeamworkService(operations, AccessRepository(database), TeamworkRepository(database))
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
        redteam,
        planning,
        swarm,
        goals,
        memory_service,
        skill_service,
        exports,
        access,
        team,
        questions,
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
    if operation.offline:
        print("network: offline (local models only, no outside tools)")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run() -> None:
    raise SystemExit(main())
