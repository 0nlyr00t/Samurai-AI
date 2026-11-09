"""Run a benchmark: the chat loop against a real host, then measure it.

The runner is deliberately thin on opinions. It builds the same operation a
TUI operator would, proves the target answers, sends the scenario's objective
into the real chat input, and then reads the operation's own event log and
graph for the numbers. Nothing is scripted, so the result reflects the harness
rather than a canned provider.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from samurai_ai import __version__
from samurai_ai.config import config_directory
from samurai_ai.domain.bench import (
    BenchError,
    BenchMetrics,
    BenchProbe,
    BenchReport,
    BenchResult,
    BenchScenario,
    BenchSuite,
    BenchVerdict,
    judge,
)
from samurai_ai.domain.ctf import HypothesisStatus
from samurai_ai.domain.graph import NodeKind, NodeState
from samurai_ai.domain.permission import PermissionMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.graph import GraphService
from samurai_ai.storage.graph import GraphRepository
from samurai_ai.tui.killchain import nodes_to_payloads, phase_for

#: how many events of a kind can be read from one operation without paging
_EVENT_LIMIT = 20000


def history_directory() -> Path:
    return config_directory() / "benchmarks"


def _load_suite(path: Path) -> BenchSuite:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchError(f"cannot read suite {path}: {exc}") from exc
    try:
        return BenchSuite.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - a bad file is a user error
        raise BenchError(f"invalid suite {path}: {exc}") from exc


class BenchRunner:
    """Drive one suite; every scenario gets a fresh operation and a temp dir."""

    def __init__(
        self,
        services: tuple[Any, ...],
        *,
        workdir: Path | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        # the tuple is what `_make_services` returns in cli order; a dataclass
        # would have to be threaded through every handler for two callers
        (
            self.operations,  # 0
            self.kernel,  # 1
            self.settings,  # 2
            self.recovery,  # 3
            self.execution,  # 4
            self.vault,  # 5
            _mcp,  # 6
            self.models,  # 7
            self.orchestrator,  # 8
            self.playbooks,  # 9
            self.reports,  # 10
            self.ctf,  # 11
            self.redteam,  # 12
            self.planning,  # 13
            self.swarm,  # 14
            self.goals,  # 15
            self.memory,  # 16
            self.skills,  # 17
            *_rest,
        ) = services
        self.graph = GraphService(
            self.operations, self.kernel, GraphRepository(self.operations.repository.database)
        )
        self.workdir = workdir or history_directory() / "runs"
        #: the visible journal of the last run, for a diagnosis when it was silent
        self.journal_tail = ""
        self.progress = progress or (lambda _line: None)

    # --- the environment -------------------------------------------------

    def canary(self, hosts: tuple[str, ...], relay: str, scenario: BenchScenario) -> None:
        """Prove the target answers before spending a run on it.

        A benchmark that cannot reach the box measures the VPN, not the harness,
        so an unreachable target is an error, never a zero-scoring run. Two
        probes: netcat when the host has it, a socket from python otherwise.
        """
        probe = scenario.probe
        if probe is None:
            return
        for host in hosts:
            command = self._probe_command(host, probe)
            argv = [*shlex.split(relay), command] if relay else ["bash", "-lc", command]
            try:
                done = subprocess.run(  # noqa: S603 - operator-supplied relay
                    argv, capture_output=True, text=True, timeout=probe.timeout_seconds + 10
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise BenchError(f"canary to {host}:{probe.port} failed: {exc}") from exc
            if done.returncode != 0:
                detail = done.stderr.strip() or done.stdout.strip()
                raise BenchError(
                    f"canary to {host}:{probe.port} refused (exit {done.returncode})"
                    + (f": {detail}" if detail else "")
                )

    @staticmethod
    def _probe_command(host: str, probe: BenchProbe) -> str:
        wait = int(probe.timeout_seconds)
        if shutil.which("nc") is not None:
            return f"nc -z -w {wait} {host} {probe.port}"
        return (
            "python3 -c "
            + shlex.quote(
                "import socket,sys\n"
                f"try:\n    socket.create_connection(({host!r}, {probe.port}), {wait}).close()\n"
                "except OSError as exc:\n    print(exc, file=sys.stderr); sys.exit(1)"
            )
        )

    # --- one scenario ----------------------------------------------------

    def run_scenario(
        self, scenario: BenchScenario, *, hosts: tuple[str, ...], relay: str, model_id: str = ""
    ) -> BenchResult:
        model_id = model_id or self.models.default_model() or self._fallback_model()
        if not model_id:
            return BenchResult(
                scenario_id=scenario.id,
                verdict=BenchVerdict.ERROR,
                error="no model: pass --model or set a default in the app",
            )
        self.settings.set_value("model.default", model_id)
        includes = (*scenario.scope_includes, *hosts)
        operation = self.operations.create(
            f"bench:{scenario.id}",
            scenario.mode,
            ScopeContract(includes=includes),
            relay=relay,
            objective=scenario.objective,
        )
        self.goals.create(
            operation.id,
            scenario.objective[:200],
            finish_line=(scenario.finish_line or scenario.objective)[:200],
            budget_steps=scenario.limits.steps,
            budget_seconds=scenario.limits.seconds,
        )
        started = time.monotonic()
        self._prepare_settings()
        # the wall ceiling is the harness timeout, not a hope: a wedged run has
        # to end as a result, or the suite never reports
        ceiling = scenario.limits.max_wall_seconds or scenario.limits.seconds * 2
        try:
            asyncio.run(asyncio.wait_for(self._drive(operation.id, scenario, model_id), ceiling))
        except Exception as exc:  # noqa: BLE001 - a broken run is a result, not a crash
            if isinstance(exc, TimeoutError) or time.monotonic() - started >= ceiling - 1:
                # cancelling the drive surfaces as the cancelled worker rather
                # than TimeoutError, so the clock is the honest witness here
                return BenchResult(
                    scenario_id=scenario.id,
                    verdict=BenchVerdict.ERROR,
                    error=f"wall ceiling {ceiling:.0f}s hit",
                )
            return BenchResult(
                scenario_id=scenario.id,
                verdict=BenchVerdict.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
        metrics = self._metrics(operation.id, started)
        if not metrics.model_calls and self.journal_tail:
            return BenchResult(
                scenario_id=scenario.id,
                verdict=BenchVerdict.ERROR,
                metrics=metrics,
                error=f"no model call went out; journal: {self.journal_tail.strip()[-300:]}",
            )
        verdict, failures = judge(metrics, scenario.limits)
        return BenchResult(
            scenario_id=scenario.id, verdict=verdict, metrics=metrics, failures=tuple(failures)
        )

    def _fallback_model(self) -> str:
        models = [model for model in self.models.list_models() if model.id]
        return models[0].id if models else ""

    def _prepare_settings(self) -> None:
        """No prompts of its own: the language screen would eat the first Enter.

        A benchmark has no operator to answer it, so the locale is settled and
        the working directory is trusted before the app mounts.
        """
        if self.settings.get_value("tui.locale") is None:
            self.settings.set_locale("en")
        try:
            trusted = json.loads(self.settings.get_value("tui.trusted_dirs") or "[]")
        except json.JSONDecodeError:
            trusted = []
        cwd = str(Path.cwd().resolve())
        if cwd not in trusted:
            self.settings.set_value("tui.trusted_dirs", json.dumps([*trusted, cwd]))

    async def _drive(self, operation_id: str, scenario: BenchScenario, model_id: str) -> None:
        from samurai_ai.tui.journal import BlockJournal

        self.progress(f"{scenario.id}: mounting")
        app = self._build_app(operation_id)
        context = app.run_test(size=(160, 44))
        pilot = await context.__aenter__()
        try:
            await pilot.pause()
            self.progress(f"{scenario.id}: ready as {app.operation.id if app.operation else '?'}")
            app._set_permission_mode(PermissionMode.YOLO)
            input_widget = app.query_one("#command-input")
            await self._send(app, pilot, input_widget, scenario.objective)
            for attempt in range(scenario.limits.stretches):
                # the turn already ran its own goal judge (FR-112); a stretch is
                # only for a goal that is still open and not blocked
                goal = self.goals.current(operation_id)
                if goal is None or goal.state.value == "done" or goal.block_reason:
                    break
                self.progress(f"{scenario.id}: stretch {attempt + 1}")
                await self._send(app, pilot, input_widget, "continue")
            self.journal_tail = app.query_one("#journal", BlockJournal).text()[-600:]
        finally:
            # model calls run in a worker thread and cannot be cancelled, so the
            # app's own teardown is bounded too: a wedged run has to report
            try:
                await asyncio.wait_for(context.__aexit__(None, None, None), 10)
            except (TimeoutError, Exception, asyncio.CancelledError):  # noqa: BLE001
                # WorkerCancelled is a CancelledError, not an Exception: without
                # catching it the teardown masks the ceiling with its own noise
                self.progress(f"{scenario.id}: teardown abandoned")

    async def _send(self, app: Any, pilot: Any, widget: Any, text: str) -> None:
        """Send one message and wait for its turn to finish.

        A key press races the worker: `wait_for_complete` right after Enter can
        find no worker yet and return while the turn is still starting, so the
        entry point the Enter handler uses is called directly and the start is
        awaited before waiting for completion.
        """
        widget.value = ""
        app._start_chat(text)
        for _ in range(120):
            if app._chat_busy:
                break
            await pilot.pause()
        await app.workers.wait_for_complete()

    def _build_app(self, operation_id: str) -> Any:
        """The app the operator would get, in its own positional order.

        The services arrive as the tuple `_make_services` returns, so the
        positions are the contract; they are listed here once, with names.
        """
        from samurai_ai.tui.app import SamuraiApp

        return SamuraiApp(
            self.operations,
            self.kernel,
            self.settings,
            self.recovery,
            self.execution,
            self.vault,
            self.orchestrator.runs,
            self.orchestrator,
            self.playbooks,
            self.reports,
            self.ctf,
            self.redteam,
            self.planning,
            self.swarm.swarms,
            self.goals,
            self.memory,
            self.skills,
            self.models,
            operation_id,
        )

    # --- the numbers -----------------------------------------------------

    def _metrics(self, operation_id: str, started: float) -> BenchMetrics:
        events = self._events(operation_id)
        calls = [event for event in events if event["type"] == "model.call"]
        runs = [event for event in events if event["type"].startswith("tool.run.")]
        failed = [
            event
            for event in runs
            if event["type"] in ("tool.run.failed", "tool.run.timeout")
        ]
        transport = [
            event
            for event in failed
            if "outside the operation scope" in str(event["payload"].get("reason", ""))
        ]
        graph = self.graph.for_operation(operation_id)
        phase = phase_for(
            self.operations.get(operation_id).mode,
            [],
            fact_payloads=nodes_to_payloads(
                [node for node in graph if node.kind is NodeKind.FACT]
            ),
            flag_count=len(self.ctf.list_flags(operation_id)),
            intent_texts=[
                node.text()
                for node in graph
                if node.kind is NodeKind.INTENT
                and node.state in (NodeState.OPEN, NodeState.RUNNING)
            ],
        )
        hypotheses = self.ctf.list_hypotheses(operation_id)
        goal = self.goals.current(operation_id)
        return BenchMetrics(
            wall_seconds=round(time.monotonic() - started, 3),
            steps_spent=goal.spent_steps if goal is not None else 0,
            model_calls=len(calls),
            prompt_tokens=sum(int(call["payload"].get("prompt_tokens", 0)) for call in calls),
            completion_tokens=sum(
                int(call["payload"].get("completion_tokens", 0)) for call in calls
            ),
            cost_usd=round(
                sum(float(call["payload"].get("estimated_cost_usd", 0.0)) for call in calls), 6
            ),
            tool_runs=len([event for event in runs if event["type"] == "tool.run.started"]),
            failed_runs=len(failed),
            transport_failures=len(transport),
            chain_phase=phase.current,
            chain_index=phase.phases.index(phase.current) if phase.current in phase.phases else 0,
            goal_reached=goal.state.value == "done" if goal is not None else False,
            open_hypotheses=sum(
                1 for item in hypotheses if item.status is HypothesisStatus.OPEN
            ),
            closed_hypotheses=sum(
                1 for item in hypotheses if item.status is not HypothesisStatus.OPEN
            ),
            flags=len(self.ctf.list_flags(operation_id)),
            artifacts=len(graph),
        )

    def _events(self, operation_id: str) -> list[dict[str, Any]]:
        database = self.operations.repository.database
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT event_type, payload FROM operation_events "
                "WHERE operation_id = ? ORDER BY created_at, id LIMIT ?",
                (operation_id, _EVENT_LIMIT),
            ).fetchall()
        return [
            {"type": row["event_type"], "payload": json.loads(row["payload"])} for row in rows
        ]

    # --- the suite -------------------------------------------------------

    def run(
        self,
        suite_path: Path,
        *,
        hosts: tuple[str, ...],
        relay: str = "",
        model_id: str = "",
        only: tuple[str, ...] = (),
    ) -> BenchReport:
        suite = _load_suite(suite_path)
        if not hosts:
            raise BenchError("no target host: pass --host (repeatable)")
        resolved_model = model_id or self.models.default_model() or self._fallback_model()
        if not resolved_model:
            raise BenchError("no model: pick one in the app or pass --model")
        # the app resolves the default itself; pin it so a run never lands on
        # the model picker and waits for an operator who is not there
        self.settings.set_value("model.default", resolved_model)
        scenarios = [item for item in suite.scenarios if not only or item.id in only]
        if not scenarios:
            raise BenchError(f"no scenario matches {only}")
        started = datetime.now(UTC)
        results: list[BenchResult] = []
        for scenario in scenarios:
            try:
                self.canary(hosts, relay, scenario)
            except BenchError as exc:
                results.append(
                    BenchResult(
                        scenario_id=scenario.id, verdict=BenchVerdict.ERROR, error=str(exc)
                    )
                )
                continue
            results.append(
                self.run_scenario(scenario, hosts=hosts, relay=relay, model_id=resolved_model)
            )
        return BenchReport(
            suite=suite.name,
            suite_version=suite.version,
            samurai_version=__version__,
            model_id=resolved_model,
            relay=relay,
            hosts=hosts,
            started_at=started,
            finished_at=datetime.now(UTC),
            results=results,
        )


def save_report(report: BenchReport) -> Path:
    """Keep the raw report and one line of history; both outside the repo."""
    directory = history_directory()
    (directory / "runs").mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.strftime("%Y%m%dT%H%M%S")
    path = directory / "runs" / f"{stamp}-{report.suite}.json"
    path.write_text(report.model_dump_json(indent=2))
    with (directory / "history.jsonl").open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "at": report.started_at.isoformat(),
                    "suite": report.suite,
                    "version": report.suite_version,
                    "samurai": report.samurai_version,
                    "model": report.model_id,
                    "relay": bool(report.relay),
                    "hosts": list(report.hosts),
                    "passed": report.passed,
                    "results": [
                        {
                            "scenario": result.scenario_id,
                            "verdict": result.verdict.value,
                            "failures": list(result.failures),
                            "wall_seconds": result.metrics.wall_seconds if result.metrics else None,
                            "chain": result.metrics.chain_phase if result.metrics else None,
                            "failed_rate": (
                                round(result.metrics.failed_rate, 3) if result.metrics else None
                            ),
                            "tokens": result.metrics.total_tokens if result.metrics else None,
                            "cost_usd": result.metrics.cost_usd if result.metrics else None,
                        }
                        for result in report.results
                    ],
                }
            )
            + "\n"
        )
    return path


def read_history(limit: int = 20) -> list[dict[str, Any]]:
    path = history_directory() / "history.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def suite_files(directory: Path | None = None) -> list[Path]:
    root = directory or Path("evals/bench")
    if not root.is_dir():
        return []
    return sorted(root.glob("*.json"))


def render_table(report: BenchReport) -> str:
    """The public metric: one line per scenario, in the order of the columns."""
    lines = [
        f"suite {report.suite}@{report.suite_version} · samurai {report.samurai_version}"
        f" · model {report.model_id}",
        f"{'scenario':<28} {'verdict':<7} {'wall':>7} {'chain':<14} {'fail':>6}"
        f" {'tok':>8} {'cost':>8}",
    ]
    for result in report.results:
        metrics = result.metrics
        lines.append(
            f"{result.scenario_id[:28]:<28} {result.verdict.value:<7}"
            f" {metrics.wall_seconds:>6.0f}s {metrics.chain_phase[:14]:<14}"
            f" {metrics.failed_rate:>5.0%} {metrics.total_tokens:>8} {metrics.cost_usd:>8.4f}"
            if metrics
            else f"{result.scenario_id[:28]:<28} {result.verdict.value:<7} {result.error or ''}"
        )
        for failure in result.failures:
            lines.append(f"    └ {failure}")
    return "\n".join(lines)


def nc_available() -> bool:
    return shutil.which("nc") is not None


__all__ = [
    "BenchRunner",
    "history_directory",
    "nc_available",
    "read_history",
    "render_table",
    "save_report",
    "suite_files",
]
