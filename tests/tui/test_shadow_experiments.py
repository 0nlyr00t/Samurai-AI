import asyncio
import json
import shlex
from pathlib import Path

import pytest
from test_app import _chat_ready_stack

from samurai_ai.agents.provider import ChatResponse, ToolCall, Usage
from samurai_ai.domain.experiments import ExperimentError, MetricDirection
from samurai_ai.domain.genes import GeneKind, GeneStatus
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.services.experiments import ExperimentService
from samurai_ai.services.genes import GeneGateError, GeneService, GeneStore
from samurai_ai.tui.app import SamuraiApp


def _service(stack, tmp_path: Path) -> ExperimentService:  # type: ignore[no-untyped-def]
    return ExperimentService(
        stack[0],
        stack[4],
        stack[5],
        GeneService(GeneStore(tmp_path / "genes.json")),
    )


def _approve_shell(stack, operation_id: str) -> None:  # type: ignore[no-untyped-def]
    approval = stack[1].request_approval(
        operation_id, "tool.run shell.exec", RiskClass.R3
    )
    stack[1].approve(approval.id, decided_by="operator")


def _metric_harness(tmp_path: Path) -> str:
    script = tmp_path / "harness.py"
    counter = tmp_path / "counter"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "values = (5, 3, 3)\n"
        "p = Path(sys.argv[1])\n"
        "index = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(index + 1))\n"
        "print(f'METRIC score={values[min(index, 2)]}')\n",
        encoding="utf-8",
    )
    return f"python3 {shlex.quote(str(script))} {shlex.quote(str(counter))}"


def test_shadow_gene_experiment_lifecycle(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    service = _service(stack, tmp_path)
    command = _metric_harness(tmp_path)
    _approve_shell(stack, operation.id)
    config = service.start(
        operation.id,
        metric="score",
        direction=MetricDirection.LOWER,
        limit=3,
        command=command,
    )
    assert stack[5].get(config.command_secret, operation.id) == command

    with pytest.raises(ExperimentError, match="baseline"):
        service.create_candidate(
            operation.id,
            slot="recon",
            kind=GeneKind.PROMPT_BLOCK,
            title="early",
            body="too early",
        )
    baseline, _output = service.run(operation.id)
    assert baseline.value == 5
    with pytest.raises(ExperimentError, match="pending"):
        service.run(operation.id)
    service.log(
        operation.id, run_id=baseline.id, status="keep", description="baseline 5"
    )

    first = service.create_candidate(
        operation.id,
        slot="recon",
        kind=GeneKind.PROMPT_BLOCK,
        title="better",
        body="improve recon",
    )
    improved_run, _output = service.run(operation.id)
    assert improved_run.value == 3
    service.log(
        operation.id, run_id=improved_run.id, status="keep", description="improved"
    )
    assert service.genes._find(service.genes.store.load(), first.id).status is GeneStatus.SHADOW

    stack[5].add("candidate-secret", "gene-secret", operation.id)
    second = service.create_candidate(
        operation.id,
        slot="recon",
        kind=GeneKind.PROMPT_BLOCK,
        title="equal",
        body="contains gene-secret",
    )
    equal_run, _output = service.run(operation.id)
    assert equal_run.value == 3
    with pytest.raises(ExperimentError, match="strict improvement"):
        service.log(operation.id, run_id=equal_run.id, status="keep", description="equal")
    final = service.log(
        operation.id,
        run_id=equal_run.id,
        status="discard",
        description="no improvement gene-secret",
    )
    assert final.status.value == "complete"
    assert final.logged_count == 3
    genes = service.genes.store.load()
    assert service.genes._find(genes, second.id).status is GeneStatus.RETIRED
    assert service.genes._find(genes, second.id).body == "contains ***REDACTED***"
    assert not service.genes.list_genes(GeneStatus.ACTIVE)

    active = service.genes.create(
        "protected", GeneKind.PROMPT_BLOCK, "active", "must not be discarded"
    )
    active.status = GeneStatus.ACTIVE
    service.genes.store.save([active])
    with pytest.raises(GeneGateError):
        service.genes.discard(active.id)

    experiment_events = [
        event
        for event in stack[0].events(operation.id)
        if event.event_type.startswith("experiment.")
    ]
    serialized = json.dumps([event.payload for event in experiment_events])
    assert command not in serialized
    assert "improve recon" not in serialized
    assert "METRIC score=" not in serialized
    assert "gene-secret" not in serialized
    assert len(stack[4].list_runs(operation.id)) == 3


def test_failed_candidate_is_retired_without_touching_active_genes(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    service = _service(stack, tmp_path)
    _approve_shell(stack, operation.id)
    service.start(
        operation.id,
        metric="score",
        direction=MetricDirection.LOWER,
        limit=2,
        command="printf 'METRIC score=1\\n'",
    )
    baseline, _output = service.run(operation.id)
    service.log(operation.id, run_id=baseline.id, status="keep", description="baseline")
    candidate = service.create_candidate(
        operation.id,
        slot="validation",
        kind=GeneKind.PLAYBOOK_NOTE,
        title="candidate",
        body="try a different validation hint",
    )
    run, _output = service.run(operation.id)
    final = service.log(
        operation.id,
        run_id=run.id,
        status="checks_failed",
        description="secondary checks failed",
    )

    assert final.status.value == "complete"
    assert service.genes._find(service.genes.store.load(), candidate.id).status is (
        GeneStatus.RETIRED
    )
    assert not service.genes.list_genes(GeneStatus.ACTIVE)


def test_pause_resume_and_dynamic_tools(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    service = _service(stack, tmp_path)
    _approve_shell(stack, operation.id)
    service.start(
        operation.id,
        metric="score",
        direction=MetricDirection.LOWER,
        limit=2,
        command="printf 'METRIC score=1\\n'",
    )
    pending, _output = service.run(operation.id)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id, experiments=service)
        async with app.run_test(size=(120, 30)):
            names = {spec.name for spec in app._tool_specs()}
            assert {"experiment_gene", "experiment_run", "experiment_log"} <= names
            app._chat_busy = True
            app._handle_experiment_command("off")
            assert service.state(operation.id).status.value == "paused"  # type: ignore[union-attr]
            assert "experiment_run" not in {spec.name for spec in app._tool_specs()}
            app._chat_busy = False
            app._start_chat = lambda _text: None  # type: ignore[method-assign]
            app._handle_experiment_command("resume")
            assert service.state(operation.id).status.value == "active"  # type: ignore[union-attr]
            service.log(
                operation.id, run_id=pending.id, status="keep", description="baseline"
            )

    asyncio.run(exercise())


def test_experiment_start_requires_approval_and_hides_command(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    service = _service(stack, tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id, experiments=service)
        started: list[str] = []

        async def allow(_screen):  # type: ignore[no-untyped-def]
            return True

        async with app.run_test(size=(120, 30)):
            app.push_screen_wait = allow  # type: ignore[method-assign]
            app._start_chat = started.append  # type: ignore[method-assign]
            command = "printf 'METRIC score=1\\n'"
            await app._start_experiment("score", MetricDirection.LOWER, 5, command)

            state = service.state(operation.id)
            assert state is not None and state.config.limit == 5
            assert started
            events = [
                event
                for event in stack[0].events(operation.id)
                if event.event_type == "experiment.started"
            ]
            assert command not in json.dumps(events[0].payload)
            assert stack[5].get(state.config.command_secret, operation.id) == command

    asyncio.run(exercise())


def test_agent_loop_auto_resumes_until_the_logged_limit(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    service = _service(stack, tmp_path)
    _approve_shell(stack, operation.id)
    service.start(
        operation.id,
        metric="score",
        direction=MetricDirection.LOWER,
        limit=2,
        command=_metric_harness(tmp_path),
    )

    class _Agent:
        def __init__(self) -> None:
            self.step = 0

        async def complete(self, request):  # type: ignore[no-untyped-def]
            usage = Usage(prompt_tokens=2, completion_tokens=1)
            if not request.tools:
                return ChatResponse(
                    text="stop", usage=usage, model_id=request.model_id, provider_id="gpt"
                )
            self.step += 1
            call: ToolCall | None = None
            text = ""
            if self.step in (1, 5):
                call = ToolCall(id=f"run-{self.step}", name="experiment_run", arguments={})
            elif self.step in (2, 6):
                state = service.state(operation.id)
                assert state is not None and state.pending_run is not None
                call = ToolCall(
                    id=f"log-{self.step}",
                    name="experiment_log",
                    arguments={
                        "run_id": state.pending_run.id,
                        "status": "keep",
                        "description": "measured improvement" if self.step == 6 else "baseline",
                    },
                )
            elif self.step == 3:
                text = "baseline logged"
            elif self.step == 4:
                call = ToolCall(
                    id="gene",
                    name="experiment_gene",
                    arguments={
                        "slot": "recon",
                        "kind": "prompt-block",
                        "title": "candidate",
                        "body": "be more precise",
                    },
                )
            else:
                text = "experiment complete"
            return ChatResponse(
                text=text,
                tool_calls=(call,) if call else (),
                usage=usage,
                model_id=request.model_id,
                provider_id="gpt",
            )

    provider = _Agent()
    stack[-1].provider_factory = lambda _provider: provider

    async def exercise() -> None:
        from textual.widgets import Input

        app = SamuraiApp(*stack, operation.id, experiments=service)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one("#command-input", Input).value = "run the experiment"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            state = service.state(operation.id)
            assert state is not None
            assert state.status.value == "complete"
            assert state.logged_count == 2
            assert state.best_metric == 3
            assert provider.step == 7
            assert not service.genes.list_genes(GeneStatus.ACTIVE)

    asyncio.run(exercise())


def test_auto_resume_yields_to_pause(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())
    service = _service(stack, tmp_path)
    service.start(
        operation.id,
        metric="score",
        direction=MetricDirection.LOWER,
        limit=2,
        command="printf 'METRIC score=1\\n'",
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id, experiments=service)
        response = ChatResponse(text="iteration summary", model_id="m", provider_id="p")
        async with app.run_test(size=(120, 30)):
            assert app._continue_experiment(operation.id, response)
            assert app._chat_history[-1].content.startswith("Bounded experiment")
            service.pause(operation.id)
            assert not app._continue_experiment(operation.id, response)

    asyncio.run(exercise())
