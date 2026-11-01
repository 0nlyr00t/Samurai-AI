"""Task constraints: small, validated, and read by the prompt before anything else."""

from __future__ import annotations

from samurai_ai.domain.constraints import (
    MAX_CONSTRAINTS,
    TEXT_MAX,
    Constraint,
    ConstraintError,
    ConstraintKind,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.constraints import ConstraintRepository


class ConstraintNotFoundError(LookupError):
    pass


class ConstraintService:
    def __init__(self, operations: OperationService, constraints: ConstraintRepository) -> None:
        self.operations = operations
        self.constraints = constraints

    def add(
        self,
        operation_id: str,
        task_id: str,
        kind: ConstraintKind,
        text: str,
        *,
        created_by: str = "operator",
    ) -> Constraint:
        self.operations.get(operation_id)
        text = " ".join(text.split())
        if not text:
            raise ConstraintError("a constraint needs text")
        if len(text) > TEXT_MAX:
            raise ConstraintError(f"constraint is longer than {TEXT_MAX} characters")
        if self.constraints.count(task_id) >= MAX_CONSTRAINTS:
            raise ConstraintError(f"task already holds {MAX_CONSTRAINTS} constraints")
        constraint = Constraint(
            operation_id=operation_id,
            task_id=task_id,
            kind=kind,
            text=text,
            created_by=created_by,
        )
        self.constraints.add(constraint)
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type="constraint.added",
                payload={"constraint_id": constraint.id, "kind": kind.value, "task_id": task_id},
            )
        )
        return constraint

    def remove(self, constraint_id: str) -> None:
        constraint = self.constraints.get(constraint_id)
        if constraint is None:
            raise ConstraintNotFoundError(f"constraint not found: {constraint_id}")
        self.constraints.remove(constraint_id)
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=constraint.operation_id,
                event_type="constraint.removed",
                payload={"constraint_id": constraint_id, "task_id": constraint.task_id},
            )
        )

    def for_task(self, task_id: str) -> list[Constraint]:
        return self.constraints.for_task(task_id)

    def block(self, task_id: str) -> str:
        """The prompt block: deny first, because prohibitions are what get missed."""
        rows = self.constraints.for_task(task_id)
        if not rows:
            return ""
        lines: list[str] = []
        for kind, title in ((ConstraintKind.DENY, "must not"), (ConstraintKind.ALLOW, "only")):
            group = [row for row in rows if row.kind is kind]
            if not group:
                continue
            lines.append(f"{title}:")
            lines += [f"- {row.text}" for row in group]
        return "\n".join(lines)

    def in_operation(self, operation_id: str) -> list[Constraint]:
        return self.constraints.for_operation(operation_id)
