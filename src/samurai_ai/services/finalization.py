"""Evidence-first finalization of an operation.

The service owns the read-side snapshot and evidence import. It does not infer
whether a hypothesis is true: the model or the operator must request every state
transition explicitly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from samurai_ai.domain.execution import SecretRedactor, ToolRunStatus
from samurai_ai.domain.graph import GraphNode, NodeKind, NodeState
from samurai_ai.domain.hypotheses import hypothesis_coverage
from samurai_ai.domain.kernel import ApprovalStatus, Artifact, ArtifactKind, KernelError
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

    @property
    def ready_to_complete(self) -> bool:
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
        allowed = ["import_artifact", "register_finding", "record_result_and_close"]
        if not coverage.unfinished_ids and not pending_execution and not pending_approvals:
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
        )

    def import_artifact(
        self,
        operation_id: str,
        path: str | Path,
        *,
        kind: ArtifactKind = ArtifactKind.OTHER,
        summary: str = "",
        task_id: str | None = None,
    ) -> Artifact:
        """Copy one trusted file into the immutable operation artifact store."""
        source = self._resolve_evidence_path(path)
        size = source.stat().st_size
        if size > self.max_artifact_bytes:
            raise ArtifactImportError(
                f"evidence file is too large: {size} bytes (limit {self.max_artifact_bytes})"
            )
        data = source.read_bytes()
        extension = source.suffix.lower().lstrip(".") or "txt"
        if not extension.isalnum() or len(extension) > 10:
            extension = "bin"
        text: str | None = None
        if extension in _TEXT_SUFFIXES:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        if text is not None:
            data = self.redactor.redact(text).text.encode("utf-8")
        return self.kernel.register_artifact(
            operation_id,
            kind,
            "" if text is None else data.decode("utf-8"),
            summary=summary[:500],
            task_id=task_id,
            created_by="agent",
            data=None if text is not None else data,
            extension=extension,
        )

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
        return (
            node.kind is NodeKind.FACT
            and node.state is NodeState.CONFIRMED
            and bool(node.payload.get("artifact_ids"))
        )


__all__ = [
    "ArtifactImportError",
    "FinalizationError",
    "FinalizationService",
    "FinalizationSnapshot",
]
