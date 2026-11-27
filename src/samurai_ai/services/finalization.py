"""Evidence-first finalization of an operation.

The service owns the read-side snapshot and evidence import. It does not infer
whether a hypothesis is true: the model or the operator must request every state
transition explicitly. It is also the one place that answers "what proof does
the finish line still lack" (``domain/completion.py``), so the chat loop, the
``/finalize`` view and the readiness report read the same verdict.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from samurai_ai.domain.completion import (
    CompletionCriterion,
    CompletionEvidence,
    completion_criterion,
    missing_requirements,
)
from samurai_ai.domain.evidence import fact_has_artifacts, run_is_proof
from samurai_ai.domain.execution import SecretRedactor, ToolRunStatus
from samurai_ai.domain.graph import GraphNode, NodeState
from samurai_ai.domain.hypotheses import hypothesis_coverage
from samurai_ai.domain.kernel import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    Finding,
    FindingSeverity,
    KernelError,
)
from samurai_ai.services.goals import GoalService
from samurai_ai.services.graph import GraphService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService

if TYPE_CHECKING:
    from samurai_ai.services.ctf import CtfService
    from samurai_ai.services.execution import ToolExecutionService


class FinalizationError(KernelError):
    """Base error for a rejected finalization action."""


class ArtifactImportError(FinalizationError):
    """An evidence path violates the import contract."""


@dataclass(frozen=True)
class FinalizationSnapshot:
    operation_id: str
    eligible_fact_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    pending_execution: tuple[str, ...]
    pending_approvals: tuple[str, ...]
    budget_steps_left: int
    budget_seconds_left: int
    allowed_actions: tuple[str, ...]
    #: what the finish line still lacks in the store; empty when proven
    missing_proof: tuple[str, ...] = ()

    @property
    def ready_to_complete(self) -> bool:
        """No open work blocks the closing path (proof is judged separately)."""
        return not (self.unresolved_ids or self.pending_execution or self.pending_approvals)


_TEXT_SUFFIXES = frozenset(
    {"txt", "log", "md", "json", "jsonl", "ndjson", "html", "xml", "csv", "yaml", "yml"}
)
_SCRATCH_ROOTS = (Path("/tmp"), Path("/var/tmp"))


class FinalizationService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        graph: GraphService,
        goals: GoalService,
        *,
        ctf: CtfService | None = None,
        execution: ToolExecutionService | None = None,
        workspace: Path | None = None,
        trusted_roots: Iterable[Path] = (),
        redactor: SecretRedactor | None = None,
        max_artifact_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self.operations = operations
        self.database = operations.repository.database
        self.kernel = kernel
        self.graph = graph
        self.goals = goals
        self.ctf = ctf
        self.execution = execution
        self.workspace = (workspace or Path.cwd()).resolve()
        self.trusted_roots = (self.workspace, *trusted_roots, *_SCRATCH_ROOTS)
        self.redactor = redactor or SecretRedactor()
        self.max_artifact_bytes = max_artifact_bytes

    def snapshot(self, operation_id: str) -> FinalizationSnapshot:
        """Return every fact and blocker needed for a truthful terminal verdict."""
        self.operations.get(operation_id)
        coverage = hypothesis_coverage(
            self.graph.for_operation(operation_id),
            self.ctf.list_hypotheses(operation_id) if self.ctf is not None else (),
        )
        eligible = tuple(
            node.id
            for node in self.graph.for_operation(operation_id)
            if self._eligible_fact(node)
        )
        pending_execution = self._pending_execution(operation_id)
        pending_approvals = tuple(
            approval.id
            for approval in self.kernel.kernel.list_approvals(
                operation_id, status=ApprovalStatus.PENDING
            )
        )
        goal = self.goals.current(operation_id)
        missing = self.missing_proof(operation_id)
        allowed = ["import_artifact", "register_finding", "record_result_and_close"]
        if (
            not coverage.unfinished_ids
            and not pending_execution
            and not pending_approvals
            and not missing
        ):
            allowed.append("complete_goal")
        return FinalizationSnapshot(
            operation_id=operation_id,
            eligible_fact_ids=eligible,
            unresolved_ids=coverage.unfinished_ids,
            pending_execution=pending_execution,
            pending_approvals=pending_approvals,
            budget_steps_left=goal.steps_left if goal is not None else 0,
            budget_seconds_left=goal.seconds_left if goal is not None else 0,
            allowed_actions=tuple(allowed),
            missing_proof=missing,
        )

    def criterion(self, operation_id: str) -> CompletionCriterion:
        """The finish line of the current goal, else its objective, else the op goal."""
        return completion_criterion(
            self.goals.current(operation_id), self.operations.get(operation_id).goal
        )

    def evidence(self, operation_id: str) -> CompletionEvidence:
        """Proof recorded since the current goal began."""
        goal = self.goals.current(operation_id)
        return CompletionEvidence.collect(
            self.graph.for_operation(operation_id),
            self.kernel.kernel.list_findings(operation_id),
            self.kernel.kernel.list_artifacts(operation_id),
            since=self.goals.evidence_since(goal) if goal is not None else None,
        )

    def missing_proof(self, operation_id: str) -> tuple[str, ...]:
        return missing_requirements(self.criterion(operation_id), self.evidence(operation_id))

    def register_finding(
        self,
        operation_id: str,
        fact_id: str,
        *,
        title: str,
        severity: FindingSeverity = FindingSeverity.INFO,
        summary: str = "",
    ) -> Finding:
        """Create and attach a finding atomically from an existing proven fact."""
        fact = self.graph.get(fact_id)
        if not self._eligible_fact(fact):
            raise FinalizationError(
                f"fact {fact_id} has no artifact or is not confirmed — evidence first"
            )
        with self.database.transaction():
            finding = self.kernel.create_finding(
                operation_id,
                title,
                severity=severity,
                summary=summary,
            )
            for artifact_id in cast("list[str]", fact.payload.get("artifact_ids") or []):
                finding = self.kernel.attach_evidence(finding.id, str(artifact_id))
            self.graph.attach_finding(fact.id, finding.id)
        return finding

    def record_result_and_close(
        self,
        operation_id: str,
        intent_id: str,
        *,
        summary: str,
        artifact_ids: Iterable[str] = (),
        run_ids: Iterable[str] = (),
        outcome: str = "done",
        reason: str = "",
        negative: bool = False,
        finding_title: str | None = None,
        severity: FindingSeverity = FindingSeverity.INFO,
        finding_summary: str = "",
    ) -> GraphNode:
        """Record a fact, optionally prove a finding, then close its intent.

        Every repository joins one outer transaction. A close or attachment
        failure therefore cannot leave a new fact without its intended result.
        """
        if finding_title is not None and outcome != "done":
            raise FinalizationError("a finding requires outcome=done")
        with self.database.transaction():
            intent = self.graph.get(intent_id)
            if intent.state is NodeState.OPEN:
                intent = self.graph.start_intent(intent.id)
            fact = self.graph.record_fact(
                intent.id,
                summary,
                tuple(artifact_ids),
                negative=negative,
                run_ids=tuple(run_ids),
            )
            if finding_title is not None:
                self.register_finding(
                    operation_id,
                    fact.id,
                    title=finding_title,
                    severity=severity,
                    summary=finding_summary,
                )
            self.graph.close_intent(intent.id, outcome, reason)
        return fact

    def import_artifact(
        self,
        operation_id: str,
        path: str | Path,
        *,
        kind: ArtifactKind = ArtifactKind.OTHER,
        summary: str = "",
        task_id: str | None = None,
        source_run_id: str | None = None,
    ) -> Artifact:
        """Copy one trusted file into the immutable operation artifact store.

        Without ``source_run_id`` the copy is the agent's own claim: anything can
        be written to a scratch directory. Naming the finished run that wrote or
        read the file binds the artifact to it — the run must belong to this
        operation and its parameters must mention the file.
        """
        source = self._resolve_evidence_path(path)
        if source_run_id is not None:
            self._require_source_run(operation_id, source_run_id, source)
        size = source.stat().st_size
        if size > self.max_artifact_bytes:
            raise ArtifactImportError(
                f"evidence file is too large: {size} bytes (limit {self.max_artifact_bytes})"
            )
        data = source.read_bytes()
        extension = source.suffix.lower().lstrip(".") or "txt"
        if not extension.isalnum() or len(extension) > 10:
            extension = "bin"
        data, text = self._redacted_payload(data, extension)
        return self.kernel.register_artifact(
            operation_id,
            kind,
            text or "",
            summary=summary[:500],
            task_id=task_id,
            created_by="agent",
            data=None if text is not None else data,
            extension=extension,
            source_run_id=source_run_id,
        )

    def _require_source_run(self, operation_id: str, run_id: str, source: Path) -> None:
        run = self.graph.runs.get_run(run_id)
        if run is None or run.operation_id != operation_id:
            raise ArtifactImportError(f"unknown run: {run_id}")
        if not run_is_proof(run, operation_id):
            raise ArtifactImportError(
                f"run {run_id} is {run.status.value}; only a finished run can vouch for a file"
            )
        mentioned = json.dumps(run.parameters, ensure_ascii=False)
        if str(source) not in mentioned and source.name not in mentioned:
            raise ArtifactImportError(
                f"run {run_id} never touched {source.name}; cite the run that wrote it"
            )

    def _redacted_payload(self, data: bytes, extension: str) -> tuple[bytes, str | None]:
        """Return immutable bytes and the text channel when it is decodable."""
        if extension not in _TEXT_SUFFIXES:
            return data, None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data, None
        redacted = self.redactor.redact(text).text
        return redacted.encode("utf-8"), redacted

    def _resolve_evidence_path(self, raw: str | Path) -> Path:
        candidate = Path(raw)
        source = (
            (self.workspace / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
        if any(
            left == ".config" and right == "samurai"
            for left, right in zip(source.parts, source.parts[1:], strict=False)
        ):
            raise ArtifactImportError("internal state is not an operation evidence workspace")
        if not any(source.is_relative_to(root.resolve()) for root in self.trusted_roots):
            raise ArtifactImportError(f"evidence path is outside trusted roots: {source}")
        if not source.is_file():
            raise ArtifactImportError(f"evidence path is not a regular file: {source}")
        return source

    def _pending_execution(self, operation_id: str) -> tuple[str, ...]:
        if self.execution is None:
            return ()
        return tuple(
            run.id
            for run in self.execution.list_runs(operation_id)
            if run.status in (ToolRunStatus.QUEUED, ToolRunStatus.RUNNING)
        )

    @staticmethod
    def _eligible_fact(node: GraphNode) -> bool:
        return fact_has_artifacts(node)


__all__ = [
    "ArtifactImportError",
    "FinalizationError",
    "FinalizationService",
    "FinalizationSnapshot",
]
