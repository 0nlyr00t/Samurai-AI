from __future__ import annotations

import argparse
import json

from samurai_ai.domain.recovery import OperationSnapshot, PlanDiff, RecoveryReport
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.settings import SettingsService


def register_recovery_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    snapshot = subcommands.add_parser("snapshot", help="Manage operation snapshots")
    snapshot_commands = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_create = snapshot_commands.add_parser("create", help="Snapshot an operation")
    snapshot_create.add_argument("operation_id")
    snapshot_create.add_argument("--label", default="")
    snapshot_list = snapshot_commands.add_parser("list", help="List snapshots")
    snapshot_list.add_argument("operation_id")
    snapshot_show = snapshot_commands.add_parser("show", help="Show a snapshot")
    snapshot_show.add_argument("snapshot_id")
    snapshot_restore = snapshot_commands.add_parser(
        "restore", help="Restore a snapshot into a new operation"
    )
    snapshot_restore.add_argument("snapshot_id")
    snapshot_restore.add_argument("--name", default=None)

    reconcile = subcommands.add_parser(
        "reconcile", help="Mark interrupted recovery actions as unknown"
    )
    reconcile.add_argument("operation_id")

    lang = subcommands.add_parser("lang", help="Show or set the interface language")
    lang.add_argument("locale", nargs="?")


def handle_recovery_command(
    args: argparse.Namespace,
    recovery: RecoveryService,
    settings: SettingsService,
) -> int | None:
    if args.command == "operation" and getattr(args, "operation_command", None) == "fork":
        operation = recovery.fork_operation(args.operation_id, name=args.name)
        if args.json_output:
            _print_json(operation.model_dump(mode="json"))
        else:
            print(
                f"{operation.id}\t{operation.mode.value}\t{operation.status.value}\t"
                f"{operation.name}"
            )
        return 0
    if args.command == "plan" and getattr(args, "plan_command", None) == "diff":
        _emit_diff(
            recovery.plan_diff(args.operation_id, args.old_plan_id, args.new_plan_id),
            args.json_output,
        )
        return 0
    if args.command == "snapshot":
        return _handle_snapshot(args, recovery)
    if args.command == "reconcile":
        report = recovery.reconcile_operation(args.operation_id)
        _emit_report(report, args.json_output)
        return 0
    if args.command == "lang":
        return _handle_lang(args, settings)
    return None


def _handle_snapshot(args: argparse.Namespace, recovery: RecoveryService) -> int:
    if args.snapshot_command == "create":
        snapshot = recovery.create_snapshot(args.operation_id, label=args.label)
        _emit_snapshot(snapshot, args.json_output)
        return 0
    if args.snapshot_command == "list":
        snapshots = recovery.list_snapshots(args.operation_id)
        if args.json_output:
            _print_json([snapshot.model_dump(mode="json") for snapshot in snapshots])
        else:
            for snapshot in snapshots:
                _print_snapshot_line(snapshot)
        return 0
    if args.snapshot_command == "show":
        snapshot = recovery.get_snapshot(args.snapshot_id)
        if args.json_output:
            _print_json(snapshot.model_dump(mode="json"))
        else:
            _print_snapshot_line(snapshot)
            data = snapshot.data
            plan = data.get("plan")
            print(f"operation: {data['operation']['id']}")
            print(f"plan: {plan['id'] if plan else '-'}")
            print(f"approvals: {len(data.get('approvals', []))}")
            print(f"findings: {len(data.get('findings', []))}")
            print(f"artifacts: {len(data.get('artifacts', []))}")
        return 0
    operation = recovery.restore_snapshot(args.snapshot_id, name=args.name)
    if args.json_output:
        _print_json(operation.model_dump(mode="json"))
    else:
        print(
            f"{operation.id}\t{operation.mode.value}\t{operation.status.value}\t"
            f"{operation.name}"
        )
    return 0


def _handle_lang(args: argparse.Namespace, settings: SettingsService) -> int:
    if args.locale is not None:
        locale = settings.set_locale(args.locale)
        if args.json_output:
            _print_json({"persisted": locale, "effective": locale})
        else:
            print(f"locale: {locale}")
        return 0
    persisted = settings.stored_locale()
    effective = settings.get_locale() or "en"
    if args.json_output:
        _print_json({"persisted": persisted, "effective": effective})
    else:
        print(f"persisted: {persisted or 'not set'}")
        print(f"effective: {effective}")
    return 0


def _emit_snapshot(snapshot: OperationSnapshot, json_output: bool) -> None:
    if json_output:
        _print_json(snapshot.model_dump(mode="json"))
    else:
        _print_snapshot_line(snapshot)


def _print_snapshot_line(snapshot: OperationSnapshot) -> None:
    print(
        f"{snapshot.id}\t{snapshot.operation_id}\t{snapshot.label or '-'}\t"
        f"{snapshot.created_at.isoformat()}"
    )


def _emit_diff(diff: PlanDiff, json_output: bool) -> None:
    if json_output:
        _print_json(diff.model_dump(mode="json"))
        return
    for section in ("tasks", "gates"):
        entity = getattr(diff, section)
        for field in ("added", "removed", "changed"):
            values = ",".join(getattr(entity, field)) or "-"
            print(f"{section}.{field}: {values}")


def _emit_report(report: RecoveryReport, json_output: bool) -> None:
    if json_output:
        _print_json(report.model_dump(mode="json"))
        return
    print(f"actions-marked-unknown: {', '.join(report.actions_marked_unknown) or '-'}")
    print(f"resumable-tasks: {', '.join(report.resumable_tasks) or '-'}")
    print(f"pending-approvals: {', '.join(report.pending_approvals) or '-'}")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
