from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from samurai_ai.domain.impact import ImpactError, ImpactLevel
from samurai_ai.domain.kernel import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    Finding,
    FindingSeverity,
    OperationPlan,
    PlanTask,
    RiskClass,
    StageGate,
)
from samurai_ai.services.impact import ImpactService
from samurai_ai.services.kernel import KernelService


def register_kernel_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    plan = subcommands.add_parser("plan", help="Manage operation plans")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    plan_create = plan_commands.add_parser(
        "create", help="Create the current plan from a JSON file or from flags"
    )
    plan_create.add_argument("operation_id")
    plan_create.add_argument("plan_file", type=Path, nargs="?")
    plan_create.add_argument("--title", default=None, help="plan title (flags mode)")
    plan_create.add_argument(
        "--task",
        action="append",
        dest="tasks",
        default=[],
        metavar="TITLE",
        help="a task title; repeat for more steps, in order",
    )
    plan_show = plan_commands.add_parser("show", help="Show the current plan")
    plan_show.add_argument("operation_id")
    plan_approve = plan_commands.add_parser("approve", help="Approve the current plan")
    plan_approve.add_argument("operation_id")
    plan_diff = plan_commands.add_parser("diff", help="Diff two plans of an operation")
    plan_diff.add_argument("operation_id")
    plan_diff.add_argument("old_plan_id")
    plan_diff.add_argument("new_plan_id")

    task = subcommands.add_parser("task", help="Manage plan tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_start = task_commands.add_parser("start", help="Start a pending task")
    task_start.add_argument("operation_id")
    task_start.add_argument("task_id")
    task_complete = task_commands.add_parser("complete", help="Complete an in-progress task")
    task_complete.add_argument("operation_id")
    task_complete.add_argument("task_id")

    gate = subcommands.add_parser("gate", help="Manage stage gates")
    gate_commands = gate.add_subparsers(dest="gate_command", required=True)
    gate_pass = gate_commands.add_parser("pass", help="Pass a gate after evidence checks")
    gate_pass.add_argument("operation_id")
    gate_pass.add_argument("gate_id")

    artifact = subcommands.add_parser("artifact", help="Manage evidence artifacts")
    artifact_commands = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_add = artifact_commands.add_parser("add", help="Register a text artifact")
    artifact_add.add_argument("operation_id")
    artifact_add.add_argument(
        "--kind", choices=[kind.value for kind in ArtifactKind], required=True
    )
    artifact_add.add_argument("--text", required=True)
    artifact_add.add_argument("--summary", default="")
    artifact_add.add_argument("--requirement")
    artifact_add.add_argument("--task", dest="task_id")
    artifact_add.add_argument("--gate", dest="gate_id")
    artifact_add.add_argument("--finding", dest="finding_id")
    artifact_list = artifact_commands.add_parser("list", help="List artifacts")
    artifact_list.add_argument("operation_id")

    finding = subcommands.add_parser("finding", help="Manage findings")
    finding_commands = finding.add_subparsers(dest="finding_command", required=True)
    finding_create = finding_commands.add_parser("create", help="Create a candidate finding")
    finding_create.add_argument("operation_id")
    finding_create.add_argument("title")
    finding_create.add_argument(
        "--severity", choices=[severity.value for severity in FindingSeverity], default="info"
    )
    finding_create.add_argument("--summary", default="")
    finding_create.add_argument("--remediation", default="")
    finding_list = finding_commands.add_parser("list", help="List findings")
    finding_list.add_argument("operation_id")
    finding_attach = finding_commands.add_parser("attach", help="Attach evidence to a finding")
    finding_attach.add_argument("finding_id")
    finding_attach.add_argument("artifact_id")
    finding_validate = finding_commands.add_parser("validate", help="Validate a finding")
    finding_validate.add_argument("finding_id")
    finding_report = finding_commands.add_parser("report", help="Mark a finding as reported")
    finding_report.add_argument("finding_id")
    finding_false_positive = finding_commands.add_parser(
        "false-positive", help="Mark a finding as false positive"
    )
    finding_false_positive.add_argument("finding_id")
    finding_duplicate = finding_commands.add_parser(
        "duplicate", help="Mark a finding as duplicate"
    )
    finding_duplicate.add_argument("finding_id")
    finding_duplicate.add_argument("original_id")
    finding_impact = finding_commands.add_parser(
        "impact", help="Escalate and grade the impact of a validated finding"
    )
    impact_commands = finding_impact.add_subparsers(
        dest="impact_command", required=True
    )
    impact_attempt = impact_commands.add_parser(
        "attempt", help="Record an escalation attempt"
    )
    impact_attempt.add_argument("finding_id")
    impact_attempt.add_argument(
        "level", choices=[level.value for level in ImpactLevel]
    )
    impact_attempt.add_argument(
        "--evidence", action="append", required=True, help="traffic pair or artifact id"
    )
    impact_attempt.add_argument("--rollback", default="")
    impact_attempt.add_argument("--refuted", action="store_true")
    impact_attempt.add_argument("--requests", type=int, default=1)
    impact_grade = impact_commands.add_parser("grade", help="Show the graded impact")
    impact_grade.add_argument("finding_id")
    finding_propose = finding_commands.add_parser(
        "propose", help="Propose a finding with dedup against the playbook ledger"
    )
    finding_propose.add_argument("operation_id")
    finding_propose.add_argument("title")
    finding_propose.add_argument(
        "--severity", choices=[severity.value for severity in FindingSeverity], default="info"
    )
    finding_propose.add_argument("--summary", default="")
    finding_propose.add_argument("--remediation", default="")
    finding_propose.add_argument("--asset", required=True)
    finding_propose.add_argument("--category", default="general")
    finding_propose.add_argument("--evidence", default="")
    finding_propose.add_argument("--force", action="store_true")

    approval = subcommands.add_parser("approval", help="Manage approvals")
    approval_commands = approval.add_subparsers(dest="approval_command", required=True)
    approval_request = approval_commands.add_parser("request", help="Request action approval")
    approval_request.add_argument("operation_id")
    approval_request.add_argument("--action", required=True)
    approval_request.add_argument(
        "--risk", choices=[risk.value for risk in RiskClass], required=True
    )
    approval_request.add_argument("--task", dest="task_id")
    approval_request.add_argument("--reason", default="")
    approval_request.add_argument("--requested-by", default="operator")
    approval_list = approval_commands.add_parser("list", help="List approvals")
    approval_list.add_argument("operation_id")
    approval_list.add_argument("--pending", action="store_true")
    approval_approve = approval_commands.add_parser(
        "approve", help="Approve one pending request, or every one with --all"
    )
    approval_approve.add_argument("approval_id", nargs="?")
    approval_approve.add_argument(
        "--all", action="store_true", help="approve every pending request (needs --operation)"
    )
    approval_approve.add_argument("--operation", dest="all_operation_id", default=None)
    approval_approve.add_argument("--actor", default="operator")
    approval_reject = approval_commands.add_parser("reject", help="Reject a pending request")
    approval_reject.add_argument("approval_id", nargs="?")
    approval_reject.add_argument("--actor", default="operator")


def handle_kernel_command(
    args: argparse.Namespace,
    kernel: KernelService,
    impact: ImpactService | None = None,
) -> int | None:
    if args.command == "plan":
        return _handle_plan(args, kernel)
    if args.command == "task":
        plan = (
            kernel.start_task(args.operation_id, _resolve_task(kernel, args))
            if args.task_command == "start"
            else kernel.complete_task(args.operation_id, _resolve_task(kernel, args))
        )
        _emit(plan, args.json_output)
        return 0
    if args.command == "gate":
        _emit(kernel.pass_gate(args.operation_id, args.gate_id), args.json_output)
        return 0
    if args.command == "artifact":
        return _handle_artifact(args, kernel)
    if args.command == "finding":
        if args.finding_command == "impact":
            if impact is None:
                print("impact needs the escalation service", file=sys.stderr)
                return 2
            return _handle_impact(args, impact)
        if args.finding_command == "propose":
            return None
        return _handle_finding(args, kernel)
    if args.command == "approval":
        return _handle_approval(args, kernel)
    return None


def _handle_plan(args: argparse.Namespace, kernel: KernelService) -> int:
    if args.plan_command == "create":
        if args.plan_file is not None and args.tasks:
            raise ValueError("give either a plan file or --task, not both")
        if args.plan_file is not None:
            title, tasks, gates = _load_plan_definition(args.plan_file)
        else:
            title, tasks, gates = _plan_from_flags(args.title, args.tasks)
        plan = kernel.create_plan(args.operation_id, title, tasks, gates)
        _emit(plan, args.json_output)
        return 0
    if args.plan_command == "show":
        current_plan = kernel.get_current_plan(args.operation_id)
        if current_plan is None:
            raise ValueError(f"operation {args.operation_id} has no current plan")
        if args.json_output:
            _print_json(current_plan.model_dump(mode="json"))
        else:
            _print_plan(current_plan)
        return 0
    _emit(kernel.approve_plan(args.operation_id), args.json_output)
    return 0


def _handle_artifact(args: argparse.Namespace, kernel: KernelService) -> int:
    if args.artifact_command == "add":
        artifact = kernel.register_artifact(
            args.operation_id,
            ArtifactKind(args.kind),
            args.text,
            summary=args.summary,
            requirement=args.requirement,
            task_id=args.task_id,
            gate_id=args.gate_id,
            finding_id=args.finding_id,
            created_by="operator",
        )
        _emit(artifact, args.json_output)
        return 0
    artifacts = kernel.kernel.list_artifacts(args.operation_id)
    if args.json_output:
        _print_json([artifact.model_dump(mode="json") for artifact in artifacts])
    else:
        for artifact in artifacts:
            links = ",".join(
                value
                for value in (
                    f"task={artifact.task_id}" if artifact.task_id else "",
                    f"gate={artifact.gate_id}" if artifact.gate_id else "",
                    f"finding={artifact.finding_id}" if artifact.finding_id else "",
                )
                if value
            )
            print(
                f"{artifact.id}\t{artifact.kind.value}\t{artifact.requirement or '-'}\t"
                f"{links or '-'}\t{artifact.summary or artifact.path}"
            )
    return 0


def _handle_finding(args: argparse.Namespace, kernel: KernelService) -> int:
    if args.finding_command == "create":
        finding = kernel.create_finding(
            args.operation_id,
            args.title,
            FindingSeverity(args.severity),
            summary=args.summary,
            remediation=args.remediation,
        )
    elif args.finding_command == "list":
        findings = kernel.kernel.list_findings(args.operation_id)
        if args.json_output:
            _print_json([finding.model_dump(mode="json") for finding in findings])
        else:
            for finding in findings:
                print(
                    f"{finding.id}\t{finding.severity.value}\t{finding.status.value}\t"
                    f"{finding.title}"
                )
        return 0
    elif args.finding_command == "attach":
        finding = kernel.attach_evidence(args.finding_id, args.artifact_id)
    elif args.finding_command == "validate":
        finding = kernel.validate_finding(args.finding_id)
    elif args.finding_command == "report":
        finding = kernel.mark_reported(args.finding_id)
    elif args.finding_command == "false-positive":
        finding = kernel.mark_false_positive(args.finding_id)
    else:
        finding = kernel.mark_duplicate(args.finding_id, args.original_id)
    _emit(finding, args.json_output)
    return 0


def _handle_impact(args: argparse.Namespace, impact: ImpactService) -> int:
    try:
        if args.impact_command == "grade":
            state = impact.grade(args.finding_id)
            level = state.level.value if state.level else "-"
            nxt = state.next_level()
            print(
                f"{state.finding_id}\tlevel={level}\tpoints={state.points}\t"
                f"reportable={'yes' if state.reportable else 'no'}\t"
                f"attempts={state.budget.attempts}/{state.budget.max_attempts}\t"
                f"next={nxt.value if nxt else '-'}"
            )
            return 0
        state = impact.record(
            args.finding_id,
            args.level,
            tuple(args.evidence),
            rollback=args.rollback,
            outcome="refuted" if args.refuted else "confirmed",
            requests=args.requests,
        )
    except ImpactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"{state.finding_id}\tlevel={state.level.value if state.level else '-'}\t"
        f"points={state.points}\tattempts={state.budget.attempts}"
    )
    return 0


def _handle_approval(args: argparse.Namespace, kernel: KernelService) -> int:
    if args.approval_command == "request":
        approval = kernel.request_approval(
            args.operation_id,
            args.action,
            RiskClass(args.risk),
            task_id=args.task_id,
            reason=args.reason,
            requested_by=args.requested_by,
        )
        _emit(approval, args.json_output)
        return 0
    elif args.approval_command == "list":
        approvals = kernel.kernel.list_approvals(
            args.operation_id,
            status=ApprovalStatus.PENDING if args.pending else None,
        )
        if args.json_output:
            _print_json([approval.model_dump(mode="json") for approval in approvals])
        else:
            for approval in approvals:
                print(
                    f"{approval.id}\t{approval.risk_class.value}\t{approval.status.value}\t"
                    f"{approval.action}"
                )
        return 0
    if args.approval_command == "approve" and args.all:
        return _approve_everything(args, kernel)
    if not args.approval_id:
        raise ValueError(f"approval {args.approval_command} needs an approval id")
    if args.approval_command == "approve":
        approval = kernel.approve(args.approval_id, args.actor)
    else:
        approval = kernel.reject(args.approval_id, args.actor)
    _emit(approval, args.json_output)
    return 0


def _approve_everything(args: argparse.Namespace, kernel: KernelService) -> int:
    """`--all`: what an operator means by "go ahead" while a run waits."""
    if not args.all_operation_id:
        raise ValueError("--all needs --operation SO_ID")
    pending = kernel.kernel.list_approvals(
        args.all_operation_id, status=ApprovalStatus.PENDING
    )
    if not pending:
        print("nothing is pending")
        return 0
    for approval in pending:
        kernel.approve(approval.id, args.actor)
        print(f"approved\t{approval.id}\t{approval.action}")
    return 0


def _resolve_task(kernel: KernelService, args: argparse.Namespace) -> str:
    """Accept a task id or a recognizable piece of its title."""
    plan = kernel.get_current_plan(args.operation_id)
    if plan is None:
        raise ValueError(f"operation {args.operation_id} has no current plan")
    wanted = args.task_id.strip()
    for task in plan.tasks:
        if task.id == wanted:
            return task.id
    matches = [task for task in plan.tasks if wanted.lower() in task.title.lower()]
    if len(matches) == 1:
        return matches[0].id
    if not matches:
        listing = ", ".join(f"{task.id}={task.title}" for task in plan.tasks)
        raise ValueError(f"no task matches {wanted!r}; try one of: {listing}")
    listing = ", ".join(f"{task.id}={task.title}" for task in matches)
    raise ValueError(f"{wanted!r} matches several tasks: {listing}")


def _plan_from_flags(
    title: str | None, titles: list[str]
) -> tuple[str, list[PlanTask], list[StageGate]]:
    """A plan typed straight on the command line: one task per `--task`, chained."""
    if not titles:
        raise ValueError("give a plan file, or --title and at least one --task")
    clean = [text.strip() for text in titles]
    if any(not text for text in clean):
        raise ValueError("a --task title cannot be empty")
    if any(len(text) > 200 for text in clean):
        raise ValueError("a --task title is long; keep it under 200 characters")
    name = (title or "").strip() or "Operation plan"
    tasks = [
        PlanTask(
            id=f"step{i}",
            title=text,
            depends_on=(f"step{i - 1}",) if i > 1 else (),
        )
        for i, text in enumerate(clean, start=1)
    ]
    return name, tasks, []


def _load_plan_definition(path: Path) -> tuple[str, list[PlanTask], list[StageGate]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("plan definition must be a JSON object")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("plan definition requires a non-empty title")
    tasks_payload = payload.get("tasks", [])
    gates_payload = payload.get("gates", [])
    if not isinstance(tasks_payload, list) or not isinstance(gates_payload, list):
        raise ValueError("plan definition fields 'tasks' and 'gates' must be lists")
    tasks = [PlanTask.model_validate(item) for item in tasks_payload]
    gates = [StageGate.model_validate(item) for item in gates_payload]
    return title, tasks, gates


_EmitModel = OperationPlan | Approval | Artifact | Finding


def _emit(model: _EmitModel, json_output: bool) -> None:
    if json_output:
        _print_json(model.model_dump(mode="json"))
    elif isinstance(model, OperationPlan):
        _print_plan(model)
    else:
        print(model)


def _print_plan(plan: OperationPlan) -> None:
    approved = "approved" if plan.approved else "not-approved"
    print(f"{plan.id}\t{approved}\t{plan.title}")
    for task in plan.tasks:
        dependencies = ",".join(task.depends_on) or "-"
        requirements = ",".join(task.required_artifacts) or "-"
        print(
            f"task\t{task.id}\t{task.status.value}\t{task.risk_class.value}\t"
            f"deps={dependencies}\tartifacts={requirements}\t{task.title}"
        )
    for gate in plan.gates:
        requirements = ",".join(gate.required_artifacts) or "-"
        print(f"gate\t{gate.id}\t{gate.status.value}\tartifacts={requirements}\t{gate.name}")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
