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


def handle_eval_command(args: argparse.Namespace) -> int | None:
    if args.command != "eval":
        return None
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
