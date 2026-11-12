"""Evaluation harness: run scripted scenario suites and compare reports.

`eval run` executes a suite JSON against the deterministic scripted provider
and emits a metrics report. `eval compare` diffs two reports and fails on
regressions (solve flips, cost/wall growth beyond tolerance, more invalid
actions, interventions or false positives). Exit codes follow the contract:
0 passed, 1 negative domain outcome (failures/regressions), 2 usage errors.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

from samurai_ai.domain.eval import compare_reports
from samurai_ai.services.eval import EvalService, load_report, load_suite, report_to_json


def register_eval_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    eval_parser = subcommands.add_parser(
        "eval", help="Run evaluation suites and compare metric reports"
    )
    eval_commands = eval_parser.add_subparsers(dest="eval_command", required=True)
    run = eval_commands.add_parser(
        "run", help="Run a suite JSON against the scripted provider"
    )
    run.add_argument("suite", type=Path, help="path to the suite JSON file")
    run.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the report JSON here; stdout when omitted",
    )
    run.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run each scenario N times and merge metrics by median",
    )
    run.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="keep scenario databases here; a temp dir when omitted",
    )
    compare = eval_commands.add_parser(
        "compare", help="Compare a current report against a baseline report"
    )
    compare.add_argument("baseline", type=Path)
    compare.add_argument("current", type=Path)
    compare.add_argument("--cost-tolerance", type=float, default=0.2)
    compare.add_argument("--wall-tolerance", type=float, default=0.3)
    compare.add_argument(
        "--min-wall-seconds",
        type=float,
        default=1.0,
        help="wall-time regression floor: baselines faster than this are never flagged",
    )
    bench = eval_commands.add_parser(
        "bench",
        help="Live benchmark: the chat loop against a real host, with the public metric",
    )
    bench.add_argument("suite", type=Path, help="path to a benchmark suite JSON")
    bench.add_argument(
        "--host",
        action="append",
        default=[],
        help="target host or address in scope (repeatable); required",
    )
    bench.add_argument("--model", default="", help="model id; the app default when omitted")
    bench.add_argument(
        "--only", action="append", default=[], help="run only this scenario id (repeatable)"
    )
    history = eval_commands.add_parser(
        "history", help="Past benchmark runs, newest last"
    )
    history.add_argument("--limit", type=int, default=20)
    bench.add_argument("--quiet", action="store_true", help="no progress lines on stderr")


def handle_eval_command(args: argparse.Namespace, services: tuple[Any, ...] = ()) -> int | None:
    if args.command != "eval":
        return None
    if args.eval_command == "bench":
        return _handle_bench(args, services)
    if args.eval_command == "history":
        return _handle_history(args)
    service = EvalService()
    if args.eval_command == "run":
        suite = load_suite(args.suite)
        if args.workdir is not None:
            report = service.run_suite(
                suite, workdir=args.workdir, repeats=args.repeat
            )
        else:
            with tempfile.TemporaryDirectory(prefix="samurai-eval-") as tmp:
                report = service.run_suite(
                    suite, workdir=Path(tmp), repeats=args.repeat
                )
        text = report_to_json(report)
        if args.out is not None:
            args.out.write_text(text, encoding="utf-8")
            print(f"wrote eval report to {args.out}")
        else:
            print(text, end="")
        for result in report.results:
            for failure in result.expectation_failures:
                print(f"{result.scenario_id}: {failure}", file=sys.stderr)
        return 0 if report.passed else 1
    baseline = load_report(args.baseline)
    current = load_report(args.current)
    comparison = compare_reports(
        baseline,
        current,
        cost_tolerance=args.cost_tolerance,
        wall_tolerance=args.wall_tolerance,
        min_wall_seconds=args.min_wall_seconds,
    )
    print(report_to_json(comparison), end="")
    for regression in comparison.regressions:
        print(f"regression: {regression}", file=sys.stderr)
    return 0 if comparison.passed else 1


def _handle_bench(args: argparse.Namespace, services: tuple[Any, ...]) -> int:
    from samurai_ai.domain.bench import BenchError
    from samurai_ai.services.bench import BenchRunner, render_table, save_report

    if not services:
        print("bench needs the service stack", file=sys.stderr)
        return 2
    runner = BenchRunner(
        services, progress=None if args.quiet else lambda line: print(line, file=sys.stderr)
    )
    try:
        report = runner.run(
            args.suite,
            hosts=tuple(args.host),
            model_id=args.model,
            only=tuple(args.only),
        )
    except BenchError as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 2
    print(render_table(report))
    path = save_report(report)
    print(f"report: {path}")
    return 0 if report.passed else 1


def _handle_history(args: argparse.Namespace) -> int:
    from samurai_ai.services.bench import read_history

    rows = read_history(args.limit)
    if not rows:
        print("no benchmark runs yet")
        return 0
    for row in rows:
        verdicts = ", ".join(
            f"{item['scenario']}={item['verdict']}" for item in row.get("results", [])
        )
        print(f"{row.get('at', '')[:19]} {row.get('suite')} {verdicts}")
        for item in row.get("results", []):
            for failure in item.get("failures", []):
                print(f"    └ {item['scenario']}: {failure}")
    return 0
