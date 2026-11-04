"""`samurai web`: read-only browser view of one operation.

The surface is a window, not a second control panel: it renders what the kernel
already holds and never writes. It binds to localhost and needs the token printed
in the link.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from samurai_ai.web.server import Surface

REPORT_MEDIA = {"pdf": "application/pdf", "html": "text/html; charset=utf-8",
                "json": "application/json", "md": "text/markdown; charset=utf-8"}


def register_web_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    web = subcommands.add_parser(
        "web", help="Serve a read-only browser view of an operation"
    )
    web.add_argument("operation_id")
    web.add_argument("--port", type=int, default=0)
    web.add_argument("--no-browser", action="store_true")
    web.add_argument(
        "--serve-for", type=float, default=0.0, help="stop after N seconds (0 = until Ctrl-C)"
    )


class _Services:
    """Adapter from the services tuple to what the surface wants."""

    def __init__(self, operation_id: str, services: tuple[Any, ...]) -> None:
        self.operation_id = operation_id
        self.operations = services[0]
        self.kernel = services[1]
        self.report_repository = services[10].reports

    def operation(self) -> dict[str, Any]:
        operation = self.operations.get(self.operation_id)
        return {
            "id": operation.id,
            "name": operation.name,
            "mode": operation.mode.value,
            "status": operation.status.value,
            "objective": operation.objective,
            "includes": list(operation.scope.includes),
            "excludes": list(operation.scope.excludes),
        }

    def counts(self) -> dict[str, int]:
        return {
            "tasks": len(self.tasks()),
            "findings": len(self.findings()),
            "reports": len(self.reports()),
        }

    def events(self, limit: int) -> list[dict[str, Any]]:
        events = self.operations.events(self.operation_id)[-limit:]
        return [
            {
                "seq": event.id,
                "type": event.event_type,
                "payload": json.dumps(event.payload, ensure_ascii=False)[:600],
            }
            for event in reversed(events)
        ]

    def tasks(self) -> list[dict[str, Any]]:
        plan = self.kernel.kernel.get_current_plan(self.operation_id)
        if plan is None:
            return []
        return [
            {"id": task.id, "status": task.status.value, "title": task.title}
            for task in plan.tasks
        ]

    def findings(self) -> list[dict[str, Any]]:
        rows = self.kernel.kernel.list_findings(self.operation_id)
        return [
            {
                "title": finding.title,
                "severity": finding.severity.value,
                "status": finding.status.value,
                "impact": getattr(finding, "impact", None),
            }
            for finding in rows
        ]

    def reports(self) -> list[dict[str, Any]]:
        return [
            {
                "id": report.id,
                "format": report.format.value,
                "size_bytes": report.size_bytes,
            }
            for report in self.report_repository.list_reports(self.operation_id)
        ]

    def report_file(self, report_id: str) -> tuple[str, bytes] | None:
        report = self.report_repository.get_report(report_id)
        if report is None or report.operation_id != self.operation_id:
            return None
        root = self.report_repository.report_root
        path = root / self.operation_id / Path(report.path).name
        if not path.exists():
            return None
        media = REPORT_MEDIA.get(report.format.value, "application/octet-stream")
        return media, path.read_bytes()


def handle_web_command(args: argparse.Namespace, services: tuple[Any, ...]) -> int | None:
    if args.command != "web":
        return None
    surface = Surface(_Services(args.operation_id, services), secrets.token_urlsafe(16))
    try:
        url = surface.start(args.port)
    except OSError as exc:
        print(f"error: cannot bind: {exc}", file=sys.stderr)
        return 1
    print(url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        if args.serve_for:
            time.sleep(args.serve_for)
        else:
            print("Ctrl-C to stop", flush=True)
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        surface.stop()
    return 0


__all__ = ["handle_web_command", "register_web_commands"]
