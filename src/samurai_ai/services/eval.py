from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from samurai_ai import __version__
from samurai_ai.agents.ollama import OllamaProvider
from samurai_ai.agents.openai_compat import OpenAICompatProvider
from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.eval import (
    EvalError,
    EvalFaultMode,
    EvalProviderFault,
    EvalScenario,
    EvalScenarioStatus,
    EvalSuite,
    EvalSuiteError,
    ScenarioMetrics,
    ScenarioResult,
    SuiteReport,
    build_aggregate,
    check_expectations,
    merge_metrics,
    metrics_from_events,
    utc_now,
)
from samurai_ai.domain.kernel import (
    ArtifactKind,
    FindingSeverity,
    PlanTask,
    RiskClass,
)
from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelDefinition,
    ProviderDefinition,
    parse_model_id,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.registry import (
    HttpFetchTool,
    LocalEchoTool,
    ProcessExecTool,
    SandboxExecTool,
    ShellExecTool,
    ToolRegistry,
)
from samurai_ai.execution.sandbox import DockerSandboxRunner, sign_allowlist
from samurai_ai.execution.security import HttpxProbeTool, NucleiScanTool
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.models import ModelService, ProviderImpl
from samurai_ai.services.operations import OperationService
from samurai_ai.services.questions import OperatorQuestionService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.swarm import SwarmService, always_approve
from samurai_ai.services.vault import VaultService, load_master_key
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.models import ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.questions import QuestionRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.swarm import SwarmRunRepository
from samurai_ai.storage.vault import VaultRepository

_EVAL_VAULT_KEY = "cd" * 32


class _ScriptedHandler(BaseHTTPRequestHandler):
    queue: list[str] = []
    executor_queue: list[str] | None = None
    fallback: str = ""
    executor_fallback: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 40
    completion_tokens: int = 8
    fault: EvalProviderFault | None = None
    calls: int = 0
    requests: list[bytes] = []
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body_in = self.rfile.read(length)
        with _ScriptedHandler.lock:
            _ScriptedHandler.requests.append(body_in)
            _ScriptedHandler.calls += 1
            calls = _ScriptedHandler.calls
            fault = _ScriptedHandler.fault
            # Executor prompts carry "Current step:" — route them to the
            # executor queue when one is configured, so swarm scenarios are
            # deterministic regardless of worker timing.
            is_executor = b"Current step:" in body_in
            if is_executor and _ScriptedHandler.executor_queue is not None:
                if _ScriptedHandler.executor_queue:
                    text = _ScriptedHandler.executor_queue.pop(0)
                else:
                    text = _ScriptedHandler.executor_fallback
            elif _ScriptedHandler.queue:
                text = _ScriptedHandler.queue.pop(0)
            else:
                text = _ScriptedHandler.fallback
            latency_ms = _ScriptedHandler.latency_ms
            prompt_tokens = _ScriptedHandler.prompt_tokens
            completion_tokens = _ScriptedHandler.completion_tokens
        if fault is not None and calls > fault.after_calls:
            self._reply_fault(fault)
            return
        if latency_ms:
            time.sleep(latency_ms / 1000)
        body = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_fault(self, fault: EvalProviderFault) -> None:
        if fault.mode is EvalFaultMode.CONNECTION_RESET:
            self.connection.shutdown(2)
            self.connection.close()
            return
        if fault.mode is EvalFaultMode.HTTP_ERROR:
            body = json.dumps({"error": {"message": "injected fault"}}).encode()
            self.send_response(fault.status)
        elif fault.mode is EvalFaultMode.MALFORMED_JSON:
            body = b'{"choices": [{"message":'
            self.send_response(200)
        else:
            body = b""
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class ScriptedProviderServer:
    """Deterministic OpenAI-compatible provider driven by a scenario script."""

    def __init__(self, scenario: EvalScenario) -> None:
        script = scenario.script
        if script is None:  # pragma: no cover - guarded by the scenario validator
            raise EvalError(f"scenario {scenario.id} has no script")
        _ScriptedHandler.queue = list(script.responses)
        _ScriptedHandler.fallback = script.responses[-1]
        _ScriptedHandler.executor_queue = (
            list(script.executor_responses)
            if script.executor_responses is not None
            else None
        )
        _ScriptedHandler.executor_fallback = (
            script.executor_responses[-1]
            if script.executor_responses
            else ""
        )
        _ScriptedHandler.latency_ms = script.latency_ms
        _ScriptedHandler.prompt_tokens = script.prompt_tokens
        _ScriptedHandler.completion_tokens = script.completion_tokens
        _ScriptedHandler.fault = script.fault
        _ScriptedHandler.calls = 0
        _ScriptedHandler.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def requests(self) -> list[bytes]:
        return list(_ScriptedHandler.requests)

    def __enter__(self) -> ScriptedProviderServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _eval_registry(workdir: Path) -> ToolRegistry:
    """The stock registry, with the sandbox bound to this workdir.

    The sandbox refuses to run without a signed allowlist, and the operator's
    global one says nothing about an eval run — so the run signs its own.
    """
    key_path = workdir / "vault.key"
    signature_path = workdir / "sandbox-allowlist.sig"
    # the vault may have been keyed from the environment, leaving no file behind;
    # the runner loads from the file, so the key has to be there
    if not key_path.exists():
        key_path.write_bytes(load_master_key(key_path))
    sign_allowlist(signature_path, key_path.read_bytes())
    runner = DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        signature_path,
        lambda: load_master_key(key_path),
    )
    return ToolRegistry(
        adapters=(
            LocalEchoTool(),
            ProcessExecTool(),
            ShellExecTool(),
            HttpFetchTool(),
            SandboxExecTool(runner),
            HttpxProbeTool(runner),
            NucleiScanTool(runner),
        )
    )


class _ScenarioStack:
    def __init__(
        self,
        workdir: Path,
        base_url: str,
        model_id: str,
        *,
        api_family: ApiFamily = ApiFamily.OPENAI_COMPATIBLE,
        api_key: str = "unused-key",
        cost_input_per_m: float = 1.0,
        cost_output_per_m: float = 2.0,
        timeout_seconds: float | None = None,
        verify_tls: bool = True,
    ) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.database = Database(workdir / "samurai.db")
        self.operations = OperationService(OperationRepository(self.database))
        self.kernel = KernelService(self.operations, KernelRepository(self.database))
        recovery_repository = RecoveryRepository(self.database)
        recovery = RecoveryService(self.operations, self.kernel, recovery_repository)
        previous_key = os.environ.get("SAMURAI_VAULT_KEY")
        os.environ["SAMURAI_VAULT_KEY"] = _EVAL_VAULT_KEY
        try:
            vault = VaultService(
                VaultRepository(self.database),
                self.operations.repository,
                workdir / "vault.key",
            )
        finally:
            if previous_key is None:
                os.environ.pop("SAMURAI_VAULT_KEY", None)
            else:
                os.environ["SAMURAI_VAULT_KEY"] = previous_key
        execution = ToolExecutionService(
            self.operations,
            self.kernel,
            recovery,
            ToolRunRepository(self.database),
            registry=_eval_registry(workdir),
        )
        endpoints = ModelEndpointRepository(self.database)
        registry = ModelRegistry(
            endpoints,
            providers=(
                ProviderDefinition(
                    id=parse_model_id(model_id)[0],
                    api_family=api_family,
                    base_url=base_url,
                    secret_name="eval-key",
                    verify_tls=verify_tls,
                ),
            ),
            models=(
                ModelDefinition(
                    id=model_id,
                    provider_id=parse_model_id(model_id)[0],
                    context_limit=128000,
                    capabilities=frozenset(
                        {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOLS}
                    ),
                    cost_input_per_m=cost_input_per_m,
                    cost_output_per_m=cost_output_per_m,
                ),
            ),
        )
        self.models = ModelService(
            registry,
            vault,
            self.kernel,
            self.operations,
            endpoints,
            recovery_repository,
            provider_factory=(
                _provider_with_timeout(api_key, api_family, timeout_seconds)
                if timeout_seconds is not None
                else None
            ),
        )
        # a reused workdir keeps the old value otherwise, which a live run notices
        if vault.repository.find_any("eval-key") is not None:
            vault.remove("eval-key")
        vault.add("eval-key", api_key)
        self.orchestrator = AgentOrchestrator(
            self.models, self.kernel, execution, self.operations,
            AgentRunRepository(self.database),
            questions=OperatorQuestionService(
                self.operations, QuestionRepository(self.database)
            ),
        )
        self.swarms = SwarmService(
            self.operations, self.kernel, self.orchestrator,
            SwarmRunRepository(self.database),
        )


def load_suite(path: Path) -> EvalSuite:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalSuiteError(f"cannot read suite file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalSuiteError(f"invalid suite JSON in {path}: {exc}") from exc
    try:
        return EvalSuite.model_validate(raw)
    except ValidationError as exc:
        raise EvalSuiteError(f"invalid suite {path}: {exc}") from exc


def _always_approve(_approval_id: str) -> bool:
    """A live run is unattended: a tool's own approval is not the thing measured."""
    return True


def _provider_with_timeout(
    api_key: str, api_family: ApiFamily, timeout_seconds: float
) -> Callable[[ProviderDefinition], ProviderImpl]:
    """A real provider that waits as long as the target says it needs.

    Local models on a loaded machine take minutes, and the stock client timeout
    would turn that into a provider error rather than a slow answer.
    """

    def build(provider: ProviderDefinition) -> ProviderImpl:
        if api_family is ApiFamily.OLLAMA:
            return OllamaProvider(provider, timeout=timeout_seconds)
        return OpenAICompatProvider(provider, api_key, timeout=timeout_seconds)

    return build


def _model_of(scenario: EvalScenario) -> str:
    """The model a scenario actually calls; the scripted id is not a model."""
    return scenario.live.model if scenario.live is not None else scenario.model_id


class EvalService:
    def run_suite(
        self,
        suite: EvalSuite,
        *,
        workdir: Path,
        repeats: int = 1,
    ) -> SuiteReport:
        if repeats < 1:
            raise EvalError(f"repeats must be >= 1, got {repeats}")
        started = utc_now()
        results = [
            self._run_scenario(suite, scenario, workdir=workdir, repeats=repeats)
            for scenario in suite.scenarios
        ]
        finished = utc_now()
        return SuiteReport(
            suite=suite.name,
            suite_version=suite.version,
            samurai_version=__version__,
            config={
                "repeats": repeats,
                "models": sorted({_model_of(scenario) for scenario in suite.scenarios}),
                "live": [scenario.id for scenario in suite.scenarios if scenario.live],
                "workers": {
                    scenario.id: scenario.workers for scenario in suite.scenarios
                },
            },
            started_at=started,
            finished_at=finished,
            results=results,
            aggregate=build_aggregate(results),
        )

    def _run_scenario(
        self,
        suite: EvalSuite,
        scenario: EvalScenario,
        *,
        workdir: Path,
        repeats: int,
    ) -> ScenarioResult:
        runs: list[ScenarioMetrics] = []
        try:
            for attempt in range(repeats):
                scenario_dir = workdir / f"{suite.name}-{scenario.id}-{attempt}"
                runs.append(self._run_once(scenario, scenario_dir))
        except Exception as exc:
            return ScenarioResult(
                scenario_id=scenario.id,
                status=EvalScenarioStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
        metrics = merge_metrics(runs)
        failures = check_expectations(metrics, scenario.expectations)
        return ScenarioResult(
            scenario_id=scenario.id,
            status=EvalScenarioStatus.PASSED if not failures else EvalScenarioStatus.FAILED,
            metrics=metrics,
            expectation_failures=failures,
        )

    def _run_once(self, scenario: EvalScenario, scenario_dir: Path) -> ScenarioMetrics:
        if scenario.live is not None:
            return self._run_live(scenario, scenario_dir)
        with ScriptedProviderServer(scenario) as provider:
            stack = _ScenarioStack(scenario_dir, provider.base_url, scenario.model_id)
            operation = stack.operations.create(
                f"eval:{scenario.id}",
                scenario.mode,
                ScopeContract(includes=scenario.scope_includes),
            )
            stack.kernel.create_plan(
                operation.id,
                f"{scenario.title} plan",
                [
                    PlanTask(id=task.id, title=task.title, risk_class=task.risk_class)
                    for task in scenario.tasks
                ],
                [],
            )
            approval = stack.kernel.request_approval(
                operation.id, f"model.call {scenario.model_id}", RiskClass.R2
            )
            stack.kernel.approve(approval.id, decided_by="eval-harness")
            # a scripted answer set rarely runs tools, and the closing gates want
            # evidence plus a result: a note artifact and a finding on it stand in
            for task in scenario.tasks:
                artifact = stack.kernel.register_artifact(
                    operation.id, ArtifactKind.NOTE, f"{scenario.id}-{task.id}", task_id=task.id
                )
                finding = stack.kernel.create_finding(
                    operation.id, f"eval {scenario.id}/{task.id}", FindingSeverity.INFO
                )
                stack.kernel.attach_evidence(finding.id, artifact.id)
            started = time.monotonic()
            if scenario.workers == 1:
                for task in scenario.tasks:
                    stack.kernel.start_task(operation.id, task.id)
                    stack.orchestrator.run_task(
                        operation.id,
                        task.id,
                        [scenario.model_id],
                        approval_id=approval.id,
                        approval_resolver=always_approve,
                        created_by="eval-harness",
                    )
            else:
                stack.swarms.start(
                    operation.id,
                    None,
                    [scenario.model_id],
                    workers=scenario.workers,
                    approval_id=approval.id,
                    approval_resolver=always_approve,
                    created_by="eval-harness",
                )
            wall = time.monotonic() - started
            return metrics_from_events(
                stack.operations.events(operation.id),
                stack.kernel.get_current_plan(operation.id),
                wall_seconds=wall,
            )

    def _run_live(self, scenario: EvalScenario, scenario_dir: Path) -> ScenarioMetrics:
        """The same run against a real model; nothing but the provider differs."""
        target = scenario.live
        if target is None:  # pragma: no cover - caller checked
            raise EvalError(f"scenario {scenario.id} has no live target")
        api_key = os.environ.get(target.secret_env, "")
        if not api_key:
            raise EvalError(
                f"scenario {scenario.id} needs {target.secret_env} in the environment"
            )
        stack = _ScenarioStack(
            scenario_dir,
            target.base_url,
            target.model,
            api_family=target.api_family,
            api_key=api_key,
            cost_input_per_m=target.cost_input_per_m,
            cost_output_per_m=target.cost_output_per_m,
            timeout_seconds=target.timeout_seconds,
            verify_tls=target.verify_tls,
        )
        operation = stack.operations.create(
            f"eval:{scenario.id}", scenario.mode, ScopeContract(includes=scenario.scope_includes)
        )
        stack.kernel.create_plan(
            operation.id,
            f"{scenario.title} plan",
            [
                PlanTask(id=task.id, title=task.title, risk_class=task.risk_class)
                for task in scenario.tasks
            ],
            [],
        )
        approval = stack.kernel.request_approval(
            operation.id, f"model.call {target.model}", RiskClass.R2
        )
        stack.kernel.approve(approval.id, decided_by="eval-harness")
        started = time.monotonic()
        if scenario.workers == 1:
            for task in scenario.tasks:
                stack.kernel.start_task(operation.id, task.id)
                stack.orchestrator.run_task(
                    operation.id,
                    task.id,
                    [target.model],
                    approval_id=approval.id,
                    created_by="eval-harness",
                    approval_resolver=_always_approve,
                )
        else:
            stack.swarms.start(
                operation.id,
                None,
                [target.model],
                workers=scenario.workers,
                approval_id=approval.id,
                created_by="eval-harness",
                approval_resolver=_always_approve,
            )
        wall = time.monotonic() - started
        return metrics_from_events(
            stack.operations.events(operation.id),
            stack.kernel.get_current_plan(operation.id),
            wall_seconds=wall,
        )


def load_report(path: Path) -> SuiteReport:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalSuiteError(f"cannot read report file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalSuiteError(f"invalid report JSON in {path}: {exc}") from exc
    try:
        return SuiteReport.model_validate(raw)
    except ValidationError as exc:
        raise EvalSuiteError(f"invalid report {path}: {exc}") from exc


def report_to_json(report: SuiteReport | Any) -> str:
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
