from __future__ import annotations

import argparse
import json
from pathlib import Path

from samurai_ai.domain.redteam import AttackStepStatus, DetectionStatus
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.services.redteam import RedTeamService


def register_redteam_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    roe = subcommands.add_parser("roe", help="Manage rules of engagement")
    roe_commands = roe.add_subparsers(dest="roe_command", required=True)
    init = roe_commands.add_parser(
        "init", help="Initialize the red team playbook for an operation"
    )
    init.add_argument("operation_id")
    init.add_argument("--threat-profile", required=True)
    init.add_argument(
        "--objective", action="append", dest="objectives", required=True
    )
    init.add_argument("--constraints", default="")
    init.add_argument("--success-criteria", default="")
    show = roe_commands.add_parser("show", help="Show the ROE")
    show.add_argument("operation_id")

    path = subcommands.add_parser("attack-path", help="Manage the attack path")
    path_commands = path.add_subparsers(dest="attack_path_command", required=True)
    step_add = path_commands.add_parser("add", help="Add an attack step")
    step_add.add_argument("operation_id")
    step_add.add_argument("--name", required=True)
    step_add.add_argument("--technique", required=True, help="ATT&CK technique id")
    step_add.add_argument("--expected-telemetry", default="")
    step_add.add_argument("--detection-criteria", default="")
    step_add.add_argument("--depends-on", default="", help="comma-separated step ids")
    step_add.add_argument("--notes", default="")
    step_list = path_commands.add_parser("list", help="List attack steps")
    step_list.add_argument("operation_id")
    step_list.add_argument(
        "--status",
        default=None,
        choices=[item.value for item in AttackStepStatus],
    )
    step_show = path_commands.add_parser("show", help="Show the ordered attack path")
    step_show.add_argument("operation_id")
    step_update = path_commands.add_parser("update", help="Transition an attack step")
    step_update.add_argument("step_id")
    step_update.add_argument(
        "--status",
        required=True,
        choices=["executing", "executed", "blocked", "skipped"],
    )
    step_update.add_argument("--notes", default="")
    step_detection = path_commands.add_parser(
        "detection", help="Record a detection outcome for an executed step"
    )
    step_detection.add_argument("step_id")
    step_detection.add_argument(
        "--result",
        required=True,
        choices=[
            DetectionStatus.DETECTED.value,
            DetectionStatus.MISSED.value,
            DetectionStatus.NOT_OBSERVED.value,
        ],
    )
    step_detection.add_argument("--notes", default="")
    step_evidence = path_commands.add_parser(
        "attach-evidence", help="Attach an evidence artifact to a step"
    )
    step_evidence.add_argument("step_id")
    step_evidence.add_argument("artifact_id")
    path_summary = path_commands.add_parser("summary", help="Show red team progress")
    path_summary.add_argument("operation_id")

    checkpoint = subcommands.add_parser("checkpoint", help="Manage stop checkpoints")
    checkpoint_commands = checkpoint.add_subparsers(
        dest="checkpoint_command", required=True
    )
    checkpoint_add = checkpoint_commands.add_parser("add", help="Add a checkpoint")
    checkpoint_add.add_argument("operation_id")
    checkpoint_add.add_argument("--name", required=True)
    checkpoint_add.add_argument("--criterion", default="")
    checkpoint_reach = checkpoint_commands.add_parser(
        "reach", help="Mark a checkpoint as reached"
    )
    checkpoint_reach.add_argument("checkpoint_id")
    checkpoint_reach.add_argument("--notes", default="")
    checkpoint_list = checkpoint_commands.add_parser("list", help="List checkpoints")
    checkpoint_list.add_argument("operation_id")

    debrief = subcommands.add_parser("debrief", help="Manage operation debriefs")
    debrief_commands = debrief.add_subparsers(dest="debrief_command", required=True)
    debrief_generate = debrief_commands.add_parser(
        "generate", help="Generate a debrief"
    )
    debrief_generate.add_argument("operation_id")
    debrief_generate.add_argument(
        "--format",
        default=ReportFormat.MARKDOWN.value,
        choices=[item.value for item in ReportFormat],
    )
    debrief_list = debrief_commands.add_parser("list", help="List debriefs")
    debrief_list.add_argument("operation_id")
    debrief_show = debrief_commands.add_parser("show", help="Print a debrief")
    debrief_show.add_argument("operation_id")
    debrief_show.add_argument("debrief_id")
    debrief_export = debrief_commands.add_parser(
        "export", help="Export a debrief with a manifest"
    )
    debrief_export.add_argument("operation_id")
    debrief_export.add_argument("debrief_id")
    debrief_export.add_argument("dest", type=Path)


def handle_redteam_command(args: argparse.Namespace, redteam: RedTeamService) -> int | None:
    if args.command == "roe":
        return _handle_roe(args, redteam)
    if args.command == "attack-path":
        return _handle_attack_path(args, redteam)
    if args.command == "checkpoint":
        return _handle_checkpoint(args, redteam)
    if args.command == "debrief":
        return _handle_debrief(args, redteam)
    return None


def _handle_roe(args: argparse.Namespace, redteam: RedTeamService) -> int:
    if args.roe_command == "init":
        plan = redteam.init_red_team(
            args.operation_id,
            args.threat_profile,
            tuple(args.objectives),
            constraints=args.constraints,
            success_criteria=args.success_criteria,
        )
        if args.json_output:
            _print_json(plan.model_dump(mode="json"))
        else:
            print(f"initialized red-team playbook: plan {plan.id}")
            for task in plan.tasks:
                print(f"task\t{task.id}\t{task.risk_class.value}\t{task.title}")
            for gate in plan.gates:
                print(f"gate\t{gate.id}\t{gate.name}")
        return 0
    roe = redteam.get_roe(args.operation_id)
    if args.json_output:
        _print_json(roe.model_dump(mode="json"))
    else:
        print(roe.describe())
    return 0


def _handle_attack_path(args: argparse.Namespace, redteam: RedTeamService) -> int:
    if args.attack_path_command == "add":
        step = redteam.add_step(
            args.operation_id,
            args.name,
            args.technique,
            expected_telemetry=args.expected_telemetry,
            detection_criteria=args.detection_criteria,
            depends_on=tuple(
                item.strip() for item in args.depends_on.split(",") if item.strip()
            ),
            notes=args.notes,
        )
        _emit(step, args.json_output)
        return 0
    if args.attack_path_command == "list":
        status = AttackStepStatus(args.status) if args.status else None
        steps = redteam.list_steps(args.operation_id, status=status)
        if args.json_output:
            _print_json([step.model_dump(mode="json") for step in steps])
        else:
            for step in steps:
                print(
                    f"{step.id}\t{step.status.value}\t{step.technique_id}\t{step.name}"
                )
        return 0
    if args.attack_path_command == "show":
        print(redteam.attack_path(args.operation_id), end="")
        return 0
    if args.attack_path_command == "update":
        action = {
            "executing": "start",
            "executed": "complete",
            "blocked": "block",
            "skipped": "skip",
        }[args.status]
        step = redteam.update_step(
            args.step_id,
            action,  # type: ignore[arg-type]
            notes=args.notes,
        )
        _emit(step, args.json_output)
        return 0
    if args.attack_path_command == "detection":
        step = redteam.record_detection(
            args.step_id, DetectionStatus(args.result), notes=args.notes
        )
        _emit(step, args.json_output)
        return 0
    if args.attack_path_command == "attach-evidence":
        step = redteam.attach_step_evidence(args.step_id, args.artifact_id)
        _emit(step, args.json_output)
        return 0
    summary = redteam.summary(args.operation_id)
    if args.json_output:
        _print_json(summary.model_dump(mode="json"))
    else:
        print(
            f"steps: {summary.steps_planned} planned, {summary.steps_executing} "
            f"executing, {summary.steps_executed} executed, "
            f"{summary.steps_blocked} blocked, {summary.steps_skipped} skipped"
        )
        print(
            f"detections: {summary.detections_detected} detected, "
            f"{summary.detections_missed} missed | checkpoints: "
            f"{summary.checkpoints_reached}/{summary.checkpoints_total} reached | "
            f"debriefs: {summary.debriefs}"
        )
    return 0


def _handle_checkpoint(args: argparse.Namespace, redteam: RedTeamService) -> int:
    if args.checkpoint_command == "add":
        checkpoint = redteam.add_checkpoint(
            args.operation_id, args.name, criterion=args.criterion
        )
        _emit(checkpoint, args.json_output)
        return 0
    if args.checkpoint_command == "reach":
        checkpoint = redteam.reach_checkpoint(args.checkpoint_id, notes=args.notes)
        _emit(checkpoint, args.json_output)
        return 0
    checkpoints = redteam.list_checkpoints(args.operation_id)
    if args.json_output:
        _print_json([item.model_dump(mode="json") for item in checkpoints])
    else:
        for item in checkpoints:
            print(f"{item.id}\t{item.status.value}\t{item.name}")
    return 0


def _handle_debrief(args: argparse.Namespace, redteam: RedTeamService) -> int:
    if args.debrief_command == "generate":
        debrief = redteam.generate_debrief(
            args.operation_id, ReportFormat(args.format)
        )
        _emit(debrief, args.json_output)
        return 0
    if args.debrief_command == "list":
        debriefs = redteam.list_debriefs(args.operation_id)
        if args.json_output:
            _print_json([item.model_dump(mode="json") for item in debriefs])
        else:
            for item in debriefs:
                print(f"{item.id}\t{item.format.value}\t{item.path}")
        return 0
    if args.debrief_command == "show":
        print(redteam.read_debrief(args.operation_id, args.debrief_id), end="")
        return 0
    manifest = redteam.export_debrief(args.operation_id, args.debrief_id, args.dest)
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
