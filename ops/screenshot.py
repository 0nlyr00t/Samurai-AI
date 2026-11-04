"""Render a real TUI screenshot for the README.

    uv run python ops/screenshot.py            # ru  -> assets/samurai-tui.ru.png
    uv run python ops/screenshot.py --lang en  # en  -> assets/samurai-tui.en.png

The shot is taken from the running app (`export_screenshot`), not drawn by hand,
so it can only show what the TUI really renders. Needs `rsvg-convert` to make the
PNG; the SVG stays next to it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SIZE = (120, 36)

JOURNAL = [
    "[events] operation created",
    "[scope] includes 127.0.0.1, juice-shop",
    "[attach] connected to the session daemon",
    "[tool] scan.nmap -sT -Pn -p 1-1000 127.0.0.1 … 3 open",
    "[approval] model.call kimi/kimi-k2-turbo-preview@v1 — waiting",
    "[agent] step 1: read the shop landing page",
    "[tool] research.read http://juice-shop/ — 200, 4 links",
    "[finding] reflected XSS in the search box (candidate)",
    "[impact] read → auth-bypass, evidence art_7f3a",
    "[agent] step 2: write the writeup",
]


def build_stack(database: Path, home: Path) -> tuple[tuple[Any, ...], Any, Any]:
    """A throwaway operation with a bit of history, nothing live."""
    os.environ["SAMURAI_HOME"] = str(home)
    from samurai_ai.cli import _make_services
    from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity, RiskClass
    from samurai_ai.domain.operations import OperationMode
    from samurai_ai.domain.scope import ScopeContract

    services = _make_services(database)
    operations, kernel = services[0], services[1]
    operation = operations.create(
        "Takeoff — HTB",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("127.0.0.1", "juice-shop")),
        objective="Foothold and proof, no persistence",
    )
    plan = kernel.create_plan(
        operation.id,
        "Takeoff",
        [
            _task("step1", "Enumerate the shop"),
            _task("step2", "Confirm the injection and grade the impact"),
            _task("step3", "Write the writeup"),
        ],
        [],
    )
    kernel.approve_plan(operation.id)
    kernel.start_task(operation.id, "step1")
    kernel.complete_task(operation.id, "step1")
    artifact = kernel.register_artifact(
        operation.id,
        ArtifactKind.COMMAND_OUTPUT,
        "HTTP/1.1 200 OK\n<title>Juice Shop</title>",
        summary="landing page",
        requirement="enumerate",
    )
    finding = kernel.create_finding(
        operation.id,
        "Reflected XSS in the search box",
        FindingSeverity.MEDIUM,
        summary="The q parameter is echoed unescaped into the results heading.",
    )
    kernel.attach_evidence(finding.id, artifact.id)
    kernel.request_approval(
        operation.id,
        "model.call kimi/kimi-k2-turbo-preview@v1",
        RiskClass.R3,
        reason="second step needs the model",
    )
    return services, operation, plan


def _task(ident: str, title: str) -> Any:
    from samurai_ai.domain.kernel import PlanTask

    return PlanTask(id=ident, title=title)


def build_app(services: tuple[Any, ...], operation_id: str) -> Any:
    """Same wiring as `samurai tui`, minus the services the app never sees."""
    from samurai_ai.tui.app import SamuraiApp

    orchestrator = services[8]
    return SamuraiApp(
        services[0],   # operations
        services[1],   # kernel
        services[2],   # settings
        services[3],   # recovery
        services[4],   # execution
        services[5],   # vault
        orchestrator.runs,
        orchestrator,
        services[9],   # playbooks
        services[10],  # reports
        services[11],  # ctf
        services[12],  # redteam
        services[13],  # planning
        services[14].swarms,
        services[15],  # goals
        services[16],  # memory
        services[17],  # skills
        services[7],   # models
        operation_id,
    )


async def shot(language: str, out: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory) / "home"
        home.mkdir()
        services, operation, _plan = build_stack(Path(directory) / "shot.db", home)
        settings = services[2]
        settings.set_locale(language)
        settings.set_value("tui.trusted_dirs", json.dumps([str(Path.cwd())]))
        app = build_app(services, operation.id)
        app.title = "Samurai AI"
        app.sub_title = ""
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal")
            for line in JOURNAL:
                journal.write(line)
            await pilot.pause()
            svg = app.export_screenshot()
            await pilot.press("ctrl+c")
    out.with_suffix(".svg").write_text(svg, encoding="utf-8")


def to_png(png_path: Path) -> None:
    """The SVG is the source; the PNG is what the README shows."""
    if shutil.which("rsvg-convert") is None:
        sys.exit("rsvg-convert not found — SVG is written, PNG is not")
    subprocess.run(
        ["rsvg-convert", "-w", "1600", str(png_path.with_suffix(".svg")), "-o", str(png_path)],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="ru", choices=["ru", "en"])
    args = parser.parse_args()
    out = ASSETS / f"samurai-tui.{args.lang}.png"
    asyncio.run(shot(args.lang, out))
    to_png(out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
