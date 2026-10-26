from __future__ import annotations

import argparse
import json
from pathlib import Path

from samurai_ai.domain.attack import ATTACK_TACTICS
from samurai_ai.domain.planning import KillChainPhase, PlannedTestStatus
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.services.exports import IntegrationExportService
from samurai_ai.services.planning import PlanningService


def register_planning_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    scenario = subcommands.add_parser(
        "scenario", help="Manage the operation planning scenario"
    )
    scenario_commands = scenario.add_subparsers(dest="scenario_command", required=True)
    init = scenario_commands.add_parser(
        "init", help="Initialize the operation planning playbook"
    )
    init.add_argument("operation_id")
    init.add_argument("--name", required=True)
    init.add_argument("--description", default="")
    init.add_argument("--schedule", default="")
    show = scenario_commands.add_parser("show", help="Show the scenario")
    show.add_argument("operation_id")
    summary = scenario_commands.add_parser("summary", help="Show planning progress")
    summary.add_argument("operation_id")
    scenario_map = scenario_commands.add_parser(
        "map", help="Show the ATT&CK/kill chain map"
    )
    scenario_map.add_argument("operation_id")
    scenario_export = scenario_commands.add_parser(
        "export-vectr", help="Export test cases as a VECTR campaign JSON"
    )
    scenario_export.add_argument("operation_id")
    scenario_export.add_argument("--dest", type=Path, required=True)

    test_case = subcommands.add_parser("test-case", help="Manage planned test cases")
    case_commands = test_case.add_subparsers(dest="test_case_command", required=True)
    case_add = case_commands.add_parser("add", help="Add a test case")
    case_add.add_argument("operation_id")
    case_add.add_argument("--name", required=True)
    case_add.add_argument("--technique", required=True, help="ATT&CK technique id")
    case_add.add_argument(
        "--phase",
        required=True,
        choices=[phase.value for phase in KillChainPhase],
    )
    case_add.add_argument("--expected-telemetry", default="")
    case_add.add_argument("--detection-expectation", default="")
    case_add.add_argument("--depends-on", default="", help="comma-separated ids")
    case_add.add_argument("--stop-before", action="store_true")
    case_add.add_argument("--notes", default="")
    case_add.add_argument(
        "--tactic", default=None, choices=list(ATTACK_TACTICS),
        help="ATT&CK tactic for the test case",
    )
    case_list = case_commands.add_parser("list", help="List test cases")
    case_list.add_argument("operation_id")
    case_list.add_argument(
        "--status",
        default=None,
        choices=[item.value for item in PlannedTestStatus],
    )
    case_update = case_commands.add_parser("update", help="Transition a test case")
    case_update.add_argument("test_case_id")
    case_update.add_argument("--status", required=True, choices=["ready", "retired"])
    case_update.add_argument("--notes", default="")

    runbook = subcommands.add_parser("runbook", help="Manage operation runbooks")
    runbook_commands = runbook.add_subparsers(dest="runbook_command", required=True)
    runbook_generate = runbook_commands.add_parser(
        "generate", help="Generate a runbook"
    )
    runbook_generate.add_argument("operation_id")
    runbook_generate.add_argument(
        "--format",
        default=ReportFormat.MARKDOWN.value,
        choices=[item.value for item in ReportFormat],
    )
    runbook_list = runbook_commands.add_parser("list", help="List runbooks")
    runbook_list.add_argument("operation_id")
    runbook_show = runbook_commands.add_parser("show", help="Print a runbook")
    runbook_show.add_argument("operation_id")
    runbook_show.add_argument("runbook_id")
    runbook_export = runbook_commands.add_parser(
        "export", help="Export a runbook with a manifest"
    )
    runbook_export.add_argument("operation_id")
    runbook_export.add_argument("runbook_id")
    runbook_export.add_argument("dest", type=Path)


def handle_planning_command(
    args: argparse.Namespace,
    planning: PlanningService,
    exports: IntegrationExportService,
) -> int | None:
    if args.command == "scenario":
        return _handle_scenario(args, planning, exports)
    if args.command == "test-case":
        return _handle_test_case(args, planning)
    if args.command == "runbook":
        return _handle_runbook(args, planning)
    return None


def _handle_scenario(
    args: argparse.Namespace,
    planning: PlanningService,
    exports: IntegrationExportService,
) -> int:
    if args.scenario_command == "init":
        plan = planning.init_planning(
            args.operation_id,
            args.name,
            description=args.description,
            schedule=args.schedule,
        )
        if args.json_output:
            _print_json(plan.model_dump(mode="json"))
        else:
            print(f"initialized operation-plan playbook: plan {plan.id}")
            for task in plan.tasks:
                print(f"task\t{task.id}\t{task.risk_class.value}\t{task.title}")
            for gate in plan.gates:
                print(f"gate\t{gate.id}\t{gate.name}")
        return 0
    if args.scenario_command == "show":
        scenario = planning.get_scenario(args.operation_id)
        if args.json_output:
            _print_json(scenario.model_dump(mode="json"))
        else:
            print(scenario.describe())
        return 0
    if args.scenario_command == "map":
        print(planning.scenario_map(args.operation_id), end="")
        return 0
    if args.scenario_command == "export-vectr":
        result = exports.export_vectr(args.operation_id, args.dest)
        if args.json_output:
            _print_json(result)
        else:
            for file in result["files"]:
                print(f"{file['path']}\tsha256={file['sha256'][:16]}…")
        return 0
    summary = planning.summary(args.operation_id)
    if args.json_output:
        _print_json(summary.model_dump(mode="json"))
    else:
        print(
            f"test cases: {summary.test_cases_draft} draft, "
            f"{summary.test_cases_ready} ready, "
            f"{summary.test_cases_retired} retired | "
            f"stop points: {summary.stop_points} | runbooks: {summary.runbooks}"
        )
    return 0


def _handle_test_case(args: argparse.Namespace, planning: PlanningService) -> int:
    if args.test_case_command == "add":
        case = planning.add_test_case(
            args.operation_id,
            args.name,
            args.technique,
            KillChainPhase(args.phase),
            expected_telemetry=args.expected_telemetry,
            detection_expectation=args.detection_expectation,
            depends_on=tuple(
                item.strip() for item in args.depends_on.split(",") if item.strip()
            ),
            stop_before=args.stop_before,
            notes=args.notes,
            tactic=args.tactic,
        )
        _emit(case, args.json_output)
        return 0
    if args.test_case_command == "list":
        status = PlannedTestStatus(args.status) if args.status else None
        cases = planning.list_test_cases(args.operation_id, status=status)
        if args.json_output:
            _print_json([case.model_dump(mode="json") for case in cases])
        else:
            for case in cases:
                print(
                    f"{case.id}\t{case.status.value}\t{case.technique_id}\t"
                    f"{case.kill_chain_phase.value}\t{case.name}"
                )
        return 0
    action = "ready" if args.status == "ready" else "retire"
    case = planning.update_test_case(
        args.test_case_id,
        action,  # type: ignore[arg-type]
        notes=args.notes,
    )
    _emit(case, args.json_output)
    return 0


def _handle_runbook(args: argparse.Namespace, planning: PlanningService) -> int:
    if args.runbook_command == "generate":
        runbook = planning.generate_runbook(
            args.operation_id, ReportFormat(args.format)
        )
        _emit(runbook, args.json_output)
        return 0
    if args.runbook_command == "list":
        runbooks = planning.list_runbooks(args.operation_id)
        if args.json_output:
            _print_json([item.model_dump(mode="json") for item in runbooks])
        else:
            for item in runbooks:
                print(f"{item.id}\t{item.format.value}\t{item.path}")
        return 0
    if args.runbook_command == "show":
        print(planning.read_runbook(args.operation_id, args.runbook_id), end="")
        return 0
    manifest = planning.export_runbook(
        args.operation_id, args.runbook_id, args.dest
    )
    if args.json_output:
        _print_json(manifest.model_dump(mode="json"))
    else:
        print(f"exported {manifest.report_id}: {len(manifest.files)} files")
    return 0


def _emit(model: object, json_output: bool) -> None:
    if json_output:
        _print_json(model.model_dump(mode="json"))  # type: ignore[attr-defined]
    else:
        print(model)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
