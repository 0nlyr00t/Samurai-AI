"""The live benchmark: canary, drive, verdict, history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from samurai_ai.agents.provider import ChatResponse, ToolCall, Usage
from samurai_ai.cli import _make_services
from samurai_ai.domain.bench import (
    BenchError,
    BenchLimits,
    BenchMetrics,
    BenchProbe,
    BenchScenario,
    BenchSuite,
    BenchVerdict,
    judge,
)
from samurai_ai.services.bench import (
    BenchRunner,
    history_directory,
    read_history,
    render_table,
    save_report,
)


class _IdleProvider:
    """One shell command, then a text answer that never claims the goal."""

    def __init__(self) -> None:
        self.calls = 0

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    async def complete(self, request):  # type: ignore[no-untyped-def]
        self.calls += 1
        if not request.tools and request.max_tokens is not None and request.max_tokens <= 8:
            return ChatResponse(
                text="continue",
                usage=Usage(prompt_tokens=5, completion_tokens=1),
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=1,
            )
        if self.calls == 1:
            return ChatResponse(
                text="осматриваю",
                tool_calls=[ToolCall(id="c1", name="shell_exec", arguments={"command": "id"})],
                usage=Usage(prompt_tokens=10, completion_tokens=4),
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=1,
            )
        return ChatResponse(
            text="пользователь известен",
            usage=Usage(prompt_tokens=20, completion_tokens=6),
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=1,
        )


def _stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    services = _make_services(tmp_path / "bench.db")
    settings, models, vault = services[2], services[7], services[5]
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    settings.set_value("tui.trusted_dirs", json.dumps([str(Path.cwd().resolve())]))
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    provider = _IdleProvider()
    models.provider_factory = lambda _provider: provider
    return services, provider


def _scenario(**limits: object) -> BenchScenario:
    payload: dict[str, object] = {
        "id": "own-host",
        "title": "look at the host",
        "objective": "осмотри хост и скажи, кто ты",
        "scope_includes": ("127.0.0.1",),
        "limits": {"steps": 6, "seconds": 120, "stretches": 0, **limits},
    }
    return BenchScenario.model_validate(payload)


def test_a_scenario_drives_the_chat_loop_and_reports_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is scripted: the real chat input, the real shell tool."""
    services, provider = _stack(tmp_path, monkeypatch)
    runner = BenchRunner(services, workdir=tmp_path / "runs")
    result = runner.run_scenario(
        _scenario(), hosts=("127.0.0.1",), relay="", model_id="gpt/test-model@v1"
    )
    assert result.verdict is BenchVerdict.FAIL  # the fake never completes the goal
    assert "goal not reached" in result.failures
    metrics = result.metrics
    assert metrics is not None
    assert metrics.tool_runs == 1
    assert metrics.failed_runs == 0
    assert metrics.transport_failures == 0
    assert metrics.model_calls >= 2
    assert provider.calls >= 2
    assert metrics.prompt_tokens > 0
    assert metrics.chain_phase  # the rail was read from the graph, not invented


def test_an_unreachable_target_is_an_error_not_a_zero_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port 1 on loopback is closed: the run must not be scored."""
    services, _provider = _stack(tmp_path, monkeypatch)
    runner = BenchRunner(services, workdir=tmp_path / "runs")
    scenario = BenchScenario.model_validate(
        {
            "id": "closed",
            "title": "closed port",
            "objective": "ничего",
            "scope_includes": ("127.0.0.1",),
            "probe": {"port": 1, "timeout_seconds": 2.0},
            "limits": {"steps": 2, "seconds": 30, "stretches": 0},
        }
    )
    suite = BenchSuite(name="s", version="1", scenarios=[scenario])
    path = tmp_path / "suite.json"
    path.write_text(suite.model_dump_json())
    report = runner.run(path, hosts=("127.0.0.1",), model_id="gpt/test-model@v1")
    assert report.results[0].verdict is BenchVerdict.ERROR
    assert "canary" in (report.results[0].error or "")


def test_a_hanging_run_ends_at_the_wall_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged run must report, not hang the suite."""

    class _Hanging(_IdleProvider):
        async def complete(self, request):  # type: ignore[no-untyped-def]
            import asyncio

            await asyncio.sleep(120)

    services, _provider = _stack(tmp_path, monkeypatch)
    services[7].provider_factory = lambda _provider: _Hanging()
    runner = BenchRunner(services, workdir=tmp_path / "runs")
    scenario = _scenario(max_wall_seconds=2.0)
    result = runner.run_scenario(
        scenario, hosts=("127.0.0.1",), relay="", model_id="gpt/test-model@v1"
    )
    assert result.verdict is BenchVerdict.ERROR
    assert "wall ceiling" in (result.error or "")


def test_a_silent_run_reports_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No model call at all means the app never sent one: say why."""
    services, _provider = _stack(tmp_path, monkeypatch)
    services[2].set_value("model.default", "")
    services[7].list_models = lambda provider_id=None: []  # type: ignore[assignment]
    runner = BenchRunner(services, workdir=tmp_path / "runs")
    result = runner.run_scenario(_scenario(), hosts=("127.0.0.1",), relay="", model_id="")
    assert result.verdict is BenchVerdict.ERROR
    assert "no model" in (result.error or "")


def test_a_host_that_is_always_up_needs_no_probe() -> None:
    runner_probe = BenchRunner._probe_command("10.0.0.5", BenchProbe(port=22, timeout_seconds=3))
    assert "10.0.0.5" in runner_probe


def test_the_suite_needs_a_host() -> None:
    runner = BenchRunner.__new__(BenchRunner)  # run() only touches models for a model id
    runner.models = type("M", (), {"default_model": staticmethod(lambda: "m@v1")})()
    suite_file = Path("evals/bench/local-smoke.json")
    with pytest.raises(BenchError, match="no target host"):
        runner.run(suite_file, hosts=())


def test_judge_counts_the_goal_the_hypotheses_and_the_noise() -> None:
    limits = BenchLimits(max_failed_rate=0.5, max_transport_failures=0)
    clean = BenchMetrics(
        wall_seconds=10.0, steps_spent=3, model_calls=4, prompt_tokens=10,
        completion_tokens=5, cost_usd=0.0, tool_runs=4, failed_runs=1,
        transport_failures=0, chain_phase="exploit", chain_index=5,
        goal_reached=True, open_hypotheses=0, closed_hypotheses=2, flags=1, artifacts=3,
    )
    assert judge(clean, limits) == (BenchVerdict.PASS, [])
    for change, expected in (
        ({"goal_reached": False}, "goal not reached"),
        ({"open_hypotheses": 2}, "untested"),
        ({"transport_failures": 1}, "transport"),
        ({"failed_runs": 4}, "failed runs"),
    ):
        verdict, failures = judge(clean.model_copy(update=change), limits)
        assert verdict is BenchVerdict.FAIL
        assert any(expected in failure for failure in failures)


def test_history_keeps_one_line_and_the_raw_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    from datetime import UTC, datetime

    from samurai_ai.domain.bench import BenchReport, BenchResult

    now = datetime.now(UTC)
    report = BenchReport(
        suite="takeoff", suite_version="1", samurai_version="0.7.1", model_id="m@v1",
        hosts=("10.0.0.5",),
        started_at=now, finished_at=now,
        results=[BenchResult(scenario_id="own-host", verdict=BenchVerdict.FAIL,
                             failures=("goal not reached",))],
    )
    path = save_report(report)
    assert path.exists()
    assert json.loads(path.read_text())["suite"] == "takeoff"
    rows = read_history()
    assert rows[-1]["suite"] == "takeoff"
    assert rows[-1]["results"][0]["failures"] == ["goal not reached"]
    assert history_directory() == tmp_path / "home" / "benchmarks"
    assert "own-host" in render_table(report)
