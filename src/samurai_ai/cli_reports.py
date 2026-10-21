from __future__ import annotations

import argparse
import json
from pathlib import Path

from samurai_ai.domain.reports import ReportFormat
from samurai_ai.services.reports import ReportService


def register_report_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    report = subcommands.add_parser("report", help="Generate and export reports")
    report_commands = report.add_subparsers(dest="report_command", required=True)
    generate = report_commands.add_parser(
        "generate", help="Generate a report from validated findings"
    )
    generate.add_argument("operation_id")
    generate.add_argument(
        "--format", choices=[item.value for item in ReportFormat], default="markdown"
    )
    generate.add_argument("--actor", default="operator")
    listing = report_commands.add_parser("list", help="List reports of an operation")
    listing.add_argument("operation_id")
    show = report_commands.add_parser("show", help="Show report metadata and content")
    show.add_argument("report_id")
    export = report_commands.add_parser(
        "export", help="Export an evidence package for a report"
    )
    export.add_argument("operation_id")
    export.add_argument("report_id")
    export.add_argument("--dest", type=Path, required=True)
    mark = report_commands.add_parser(
        "mark-reported", help="Mark validated findings as reported"
    )
    mark.add_argument("operation_id")
    mark.add_argument("finding_ids", nargs="+")


def handle_report_command(
    args: argparse.Namespace, reports: ReportService
) -> int | None:
    if args.command != "report":
        return None
    if args.report_command == "generate":
        report = reports.generate(
            args.operation_id, ReportFormat(args.format), created_by=args.actor
        )
        _emit(report, args.json_output)
        return 0
    if args.report_command == "list":
        items = reports.reports.list_reports(args.operation_id)
        if args.json_output:
            _print_json([item.model_dump(mode="json") for item in items])
        else:
            for item in items:
                print(
                    f"{item.id}\t{item.format.value}\tfindings={len(item.finding_ids)}\t"
                    f"{item.path}"
                )
        return 0
    if args.report_command == "show":
        existing = reports.reports.get_report(args.report_id)
        if existing is None:
            raise LookupError(f"report not found: {args.report_id}")
        if args.json_output:
            _print_json(existing.model_dump(mode="json"))
        else:
            print(
                f"{existing.id}\t{existing.format.value}\t{existing.created_at.isoformat()}"
            )
            content = (reports.reports.report_root.parent / existing.path).read_text(
                encoding="utf-8"
            )
            print(content)
        return 0
    if args.report_command == "export":
        manifest = reports.export(args.operation_id, args.report_id, args.dest)
        if args.json_output:
            _print_json(manifest.model_dump(mode="json"))
        else:
            print(f"exported {len(manifest.files)} files to {args.dest}")
        return 0
    updated = reports.mark_reported(args.operation_id, args.finding_ids)
    if args.json_output:
        _print_json([finding.model_dump(mode="json") for finding in updated])
    else:
        for finding in updated:
            print(f"{finding.id}\t{finding.status.value}\t{finding.title}")
    return 0


def _emit(model: object, json_output: bool) -> None:
    if json_output:
        _print_json(model.model_dump(mode="json"))  # type: ignore[attr-defined]
    else:
        print(model)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
