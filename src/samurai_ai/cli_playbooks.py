from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from samurai_ai.domain.kernel import FindingSeverity
from samurai_ai.domain.playbooks import AssetKind
from samurai_ai.services.playbooks import PlaybookService


def register_playbook_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    playbook = subcommands.add_parser("playbook", help="Manage product playbooks")
    playbook_commands = playbook.add_subparsers(dest="playbook_command", required=True)
    init = playbook_commands.add_parser(
        "init", help="Initialize the bug bounty playbook for an operation"
    )
    init.add_argument("operation_id")
    init.add_argument("--policy-url", default="")
    init.add_argument(
        "--asset-types",
        default=",".join(kind.value for kind in AssetKind),
        help="comma-separated asset kinds allowed by the program",
    )
    init.add_argument("--safe-harbor", action="store_true")
    init.add_argument("--notes", default="")
    show = playbook_commands.add_parser("show", help="Show program rules")
    show.add_argument("operation_id")
    summary = playbook_commands.add_parser("summary", help="Show playbook summary")
    summary.add_argument("operation_id")
    install = playbook_commands.add_parser(
        "install", help="Install a custom playbook template from JSON"
    )
    install.add_argument("playbook_file", type=Path)
    use = playbook_commands.add_parser(
        "use", help="Create the operation plan from an installed custom playbook"
    )
    use.add_argument("operation_id")
    use.add_argument("name")
    playbook_commands.add_parser("list-custom", help="List installed custom playbooks")

    asset = subcommands.add_parser("asset", help="Manage the bug bounty asset ledger")
    asset_commands = asset.add_subparsers(dest="asset_command", required=True)
    asset_add = asset_commands.add_parser("add", help="Record an in-scope asset")
    asset_add.add_argument("operation_id")
    asset_add.add_argument("kind", choices=[kind.value for kind in AssetKind])
    asset_add.add_argument("value")
    asset_add.add_argument("--source", default="operator")
    asset_retire = asset_commands.add_parser("retire", help="Retire an asset")
    asset_retire.add_argument("operation_id")
    asset_retire.add_argument("kind", choices=[kind.value for kind in AssetKind])
    asset_retire.add_argument("value")
    asset_list = asset_commands.add_parser("list", help="List active assets")
    asset_list.add_argument("operation_id")
    asset_list.add_argument(
        "--all", action="store_true", help="include retired assets"
    )
    asset_diff = asset_commands.add_parser("diff", help="Diff the asset ledger")
    asset_diff.add_argument("operation_id")
    asset_diff.add_argument(
        "--since", default=None, help="ISO timestamp to diff against"
    )


def handle_playbook_command(
    args: argparse.Namespace, playbooks: PlaybookService
) -> int | None:
    if args.command == "finding" and getattr(args, "finding_command", None) == "propose":
        finding = playbooks.propose_finding(
            args.operation_id,
            args.title,
            FindingSeverity(args.severity),
            summary=args.summary,
            remediation=args.remediation,
            affected_asset=args.asset,
            category=args.category,
            evidence_ids=tuple(
                item for item in args.evidence.split(",") if item.strip()
            ),
            force=args.force,
        )
        _emit(finding, args.json_output)
        return 0
    if args.command == "playbook":
        return _handle_playbook(args, playbooks)
    if args.command == "asset":
        return _handle_asset(args, playbooks)
    return None


def _handle_playbook(args: argparse.Namespace, playbooks: PlaybookService) -> int:
    if args.playbook_command == "init":
        asset_types = tuple(
            AssetKind(item.strip())
            for item in args.asset_types.split(",")
            if item.strip()
        )
        plan = playbooks.init_bug_bounty(
            args.operation_id,
            policy_url=args.policy_url,
            asset_types=asset_types,
            safe_harbor=args.safe_harbor,
            notes=args.notes,
        )
        if args.json_output:
            _print_json(plan.model_dump(mode="json"))
        else:
            print(f"initialized bug-bounty playbook: plan {plan.id}")
            for task in plan.tasks:
                print(f"task\t{task.id}\t{task.risk_class.value}\t{task.title}")
            for gate in plan.gates:
                print(f"gate\t{gate.id}\t{gate.name}")
        return 0
    if args.playbook_command == "install":
        template = playbooks.install_custom(args.playbook_file)
        if args.json_output:
            _print_json(template.model_dump(mode="json"))
        else:
            print(
                f"installed custom playbook {template.name}: "
                f"{len(template.tasks)} tasks, {len(template.gates)} gates"
            )
        return 0
    if args.playbook_command == "use":
        plan = playbooks.use_custom(args.operation_id, args.name)
        if args.json_output:
            _print_json(plan.model_dump(mode="json"))
        else:
            print(f"created plan {plan.id} from custom playbook {args.name}")
            for task in plan.tasks:
                print(f"task\t{task.id}\t{task.risk_class.value}\t{task.title}")
            for gate in plan.gates:
                print(f"gate\t{gate.id}\t{gate.name}")
        return 0
    if args.playbook_command == "list-custom":
        templates = playbooks.list_custom()
        if args.json_output:
            _print_json([template.model_dump(mode="json") for template in templates])
        else:
            for template in templates:
                print(
                    f"{template.name}\t{template.title}\t"
                    f"tasks={len(template.tasks)}\tgates={len(template.gates)}"
                )
        return 0
    if args.playbook_command == "show":
        rules = playbooks.get_rules(args.operation_id)
        if args.json_output:
            _print_json(rules.model_dump(mode="json"))
        else:
            print(rules.describe())
        return 0
    summary = playbooks.summary(args.operation_id)
    if args.json_output:
        _print_json(summary.model_dump(mode="json"))
    else:
        print(
            f"assets: {summary.assets_active} active, {summary.assets_retired} retired"
        )
        print(
            f"findings: {summary.findings_total} total, "
            f"{summary.findings_validated} validated, "
            f"{summary.findings_duplicate} duplicates, "
            f"{summary.findings_reported} reported"
        )
    return 0


def _handle_asset(args: argparse.Namespace, playbooks: PlaybookService) -> int:
    if args.asset_command == "add":
        entry = playbooks.record_asset(
            args.operation_id,
            AssetKind(args.kind),
            args.value,
            source=args.source,
        )
        _emit(entry, args.json_output)
        return 0
    if args.asset_command == "retire":
        playbooks.retire_asset(args.operation_id, AssetKind(args.kind), args.value)
        print(f"retired {args.kind} {args.value}")
        return 0
    if args.asset_command == "list":
        entries = playbooks.list_assets(
            args.operation_id, active_only=not args.all
        )
        if args.json_output:
            _print_json([entry.model_dump(mode="json") for entry in entries])
        else:
            for entry in entries:
                print(f"{entry.id}\t{entry.kind.value}\t{entry.value}\t{entry.source}")
        return 0
    since = datetime.fromisoformat(args.since) if args.since else None
    diff = playbooks.asset_diff(args.operation_id, since=since)
    if args.json_output:
        _print_json(diff.model_dump(mode="json"))
    else:
        for label, group in (
            ("added", diff.added),
            ("removed", diff.removed),
            ("unchanged", diff.unchanged),
        ):
            for entry in group:
                print(f"{label}\t{entry.kind.value}\t{entry.value}")
    return 0


def _emit(model: object, json_output: bool) -> None:
    if json_output:
        _print_json(model.model_dump(mode="json"))  # type: ignore[attr-defined]
    else:
        print(model)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
