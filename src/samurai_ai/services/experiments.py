"""Governed benchmark runs and shadow-gene experiment lifecycle."""

from __future__ import annotations

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.experiments import (
    MAX_EXPERIMENT_DESCRIPTION,
    ExperimentConfig,
    ExperimentError,
    ExperimentRun,
    ExperimentState,
    ExperimentStatus,
    MetricDirection,
    improved,
    new_experiment_id,
    new_experiment_run_id,
    parse_metric,
)
from samurai_ai.domain.genes import Gene, GeneError, GeneKind, GeneStatus
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.genes import GeneService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService


class ExperimentService:
    STARTED = "experiment.started"
    CANDIDATE = "experiment.candidate"
    RUN = "experiment.run.finished"
    LOGGED = "experiment.logged"
    PAUSED = "experiment.paused"
    RESUMED = "experiment.resumed"
    COMPLETED = "experiment.completed"

    def __init__(
        self,
        operations: OperationService,
        execution: ToolExecutionService,
        vault: VaultService,
        genes: GeneService,
    ) -> None:
        self.operations = operations
        self.execution = execution
        self.vault = vault
        self.genes = genes

    def state(self, operation_id: str) -> ExperimentState | None:
        state: ExperimentState | None = None
        for event in self.operations.events(operation_id):
            if event.event_type == self.STARTED:
                try:
                    config = ExperimentConfig(
                        id=str(event.payload["id"]),
                        metric=str(event.payload["metric"]),
                        direction=MetricDirection(str(event.payload["direction"])),
                        limit=int(event.payload["limit"]),
                        command_secret=str(event.payload["command_secret"]),
                    )
                except (KeyError, ValueError) as exc:
                    raise ExperimentError(f"invalid experiment event: {exc}") from exc
                state = ExperimentState(config=config)
            elif state is None:
                continue
            elif event.event_type == self.CANDIDATE:
                state.open_candidate = str(event.payload["gene_id"])
            elif event.event_type == self.RUN:
                state.runs.append(
                    ExperimentRun(
                        id=str(event.payload["id"]),
                        iteration=int(event.payload["iteration"]),
                        tool_run_id=str(event.payload["tool_run_id"]),
                        exit_code=(
                            int(event.payload["exit_code"])
                            if event.payload.get("exit_code") is not None
                            else None
                        ),
                        value=(
                            float(event.payload["value"])
                            if event.payload.get("value") is not None
                            else None
                        ),
                        gene_id=(
                            str(event.payload["gene_id"])
                            if event.payload.get("gene_id")
                            else None
                        ),
                    )
                )
                state.open_candidate = None
            elif event.event_type == self.LOGGED:
                run = self._run(state, str(event.payload["run_id"]))
                run.status = str(event.payload["status"])
                run.description = str(event.payload.get("description", ""))
                if run.status == "keep":
                    state.best_metric = run.value
                    state.best_run_id = run.id
                    state.best_gene_id = run.gene_id
                if state.logged_count >= state.config.limit:
                    state.status = ExperimentStatus.COMPLETE
            elif event.event_type == self.PAUSED:
                state.status = ExperimentStatus.PAUSED
            elif event.event_type == self.RESUMED:
                state.status = ExperimentStatus.ACTIVE
            elif event.event_type == self.COMPLETED:
                state.status = ExperimentStatus.COMPLETE
        return state

    def start(
        self,
        operation_id: str,
        *,
        metric: str,
        direction: MetricDirection,
        limit: int,
        command: str,
    ) -> ExperimentConfig:
        self.operations.get(operation_id)
        existing = self.state(operation_id)
        if existing is not None and existing.status is not ExperimentStatus.COMPLETE:
            raise ExperimentError("an unfinished experiment already exists")
        experiment_id = new_experiment_id()
        secret_name = f"experiment-{experiment_id.removeprefix('exp_')}-command"
        config = ExperimentConfig(
            id=experiment_id,
            metric=metric,
            direction=direction,
            limit=limit,
            command_secret=secret_name,
        )
        if not command.strip():
            raise ExperimentError("experiment command is empty")
        self.vault.add(secret_name, command, operation_id, created_by="operator")
        self._event(
            operation_id,
            self.STARTED,
            {
                "id": config.id,
                "metric": config.metric,
                "direction": config.direction.value,
                "limit": config.limit,
                "command_secret": config.command_secret,
            },
        )
        return config

    def create_candidate(
        self,
        operation_id: str,
        *,
        slot: str,
        kind: GeneKind,
        title: str,
        body: str,
    ) -> Gene:
        state = self._active(operation_id)
        if state.pending_run is not None:
            raise ExperimentError("log the pending run before creating a candidate")
        if state.open_candidate is not None:
            raise ExperimentError("benchmark the current candidate before creating another")
        if state.best_metric is None:
            raise ExperimentError("run and keep a baseline before creating candidates")
        redactor = self._redactor(operation_id)
        gene = self.genes.create(
            slot,
            kind,
            redactor.redact(title).text,
            redactor.redact(body).text,
            notes=f"experiment {state.config.id}",
        )
        self._event(operation_id, self.CANDIDATE, {"gene_id": gene.id})
        return gene

    def run(self, operation_id: str) -> tuple[ExperimentRun, str]:
        state = self._active(operation_id)
        if state.pending_run is not None:
            raise ExperimentError("log the pending run before another benchmark")
        if state.best_metric is not None and state.open_candidate is None:
            raise ExperimentError("create a shadow candidate before the next benchmark")
        command = self.vault.get(state.config.command_secret, operation_id)
        self.execution.redactor.register(command)
        result = self.execution.run_tool(
            operation_id, "shell.exec", {"command": command}
        )
        value = parse_metric(result.stdout, state.config.metric)
        run = ExperimentRun(
            id=new_experiment_run_id(),
            iteration=state.logged_count + 1,
            tool_run_id=result.run.id,
            exit_code=result.run.exit_code,
            value=value,
            gene_id=state.open_candidate,
        )
        self._event(
            operation_id,
            self.RUN,
            {
                "id": run.id,
                "iteration": run.iteration,
                "tool_run_id": run.tool_run_id,
                "exit_code": run.exit_code,
                "value": run.value,
                "gene_id": run.gene_id,
            },
        )
        return run, result.stdout[-4000:]

    def log(
        self,
        operation_id: str,
        *,
        run_id: str,
        status: str,
        description: str,
    ) -> ExperimentState:
        state = self._active(operation_id)
        run = self._run(state, run_id)
        if run.status is not None:
            raise ExperimentError("experiment run is already logged")
        if status not in ("keep", "discard", "crash", "checks_failed"):
            raise ExperimentError("invalid experiment result status")
        if status == "keep":
            if run.exit_code != 0 or run.value is None:
                raise ExperimentError("keep requires exit 0 and the configured finite metric")
            if not improved(run.value, state.best_metric, state.config.direction):
                raise ExperimentError("keep requires a strict improvement over the best metric")
        if status != "keep" and run.gene_id:
            self.genes.discard(run.gene_id)
        redacted = self._redactor(operation_id).redact(description).text
        self._event(
            operation_id,
            self.LOGGED,
            {
                "run_id": run.id,
                "status": status,
                "description": redacted[:MAX_EXPERIMENT_DESCRIPTION],
            },
        )
        updated = self._state(operation_id)
        if updated.logged_count >= updated.config.limit:
            self._event(operation_id, self.COMPLETED, {"runs": updated.logged_count})
            updated.status = ExperimentStatus.COMPLETE
        return updated

    def pause(self, operation_id: str) -> ExperimentState:
        state = self._active(operation_id)
        self._event(operation_id, self.PAUSED, {"pending": state.pending_run is not None})
        return self._state(operation_id)

    def resume(self, operation_id: str) -> ExperimentState:
        state = self._state(operation_id)
        if state.status is not ExperimentStatus.PAUSED:
            raise ExperimentError("only a paused experiment can resume")
        if state.remaining <= 0:
            raise ExperimentError("experiment iteration limit is exhausted")
        self._event(operation_id, self.RESUMED)
        return self._active(operation_id)

    def _active(self, operation_id: str) -> ExperimentState:
        state = self._state(operation_id)
        if state.status is not ExperimentStatus.ACTIVE:
            raise ExperimentError(f"experiment is {state.status.value}")
        if state.remaining <= 0:
            raise ExperimentError("experiment iteration limit is exhausted")
        return state

    def _state(self, operation_id: str) -> ExperimentState:
        state = self.state(operation_id)
        if state is None:
            raise ExperimentError("no experiment configured")
        return state

    @staticmethod
    def _run(state: ExperimentState, run_id: str) -> ExperimentRun:
        for run in state.runs:
            if run.id == run_id:
                return run
        raise ExperimentError(f"experiment run not found: {run_id}")

    def _redactor(self, operation_id: str) -> SecretRedactor:
        redactor = SecretRedactor()
        for metadata in self.vault.list_metadata(operation_id):
            redactor.register(self.vault.get(metadata.name, operation_id))
        return redactor

    def _event(
        self,
        operation_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=event_type,
                payload=payload or {},
            )
        )


__all__ = ["ExperimentService", "ExperimentError", "GeneError", "GeneStatus"]
