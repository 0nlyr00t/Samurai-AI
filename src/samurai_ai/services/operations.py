from __future__ import annotations

from samurai_ai.domain.operations import (
    Operation,
    OperationEvent,
    OperationMode,
    OperationStatus,
)
from samurai_ai.domain.scope import ScopeContract, ScopeDecision
from samurai_ai.storage.operations import OperationRepository


class OperationNotFoundError(LookupError):
    pass


class OperationService:
    def __init__(self, repository: OperationRepository) -> None:
        self.repository = repository

    def create(
        self,
        name: str,
        mode: OperationMode,
        scope: ScopeContract,
        *,
        offline: bool = False,
        objective: str = "",
    ) -> Operation:
        operation = Operation(
            name=name,
            mode=mode,
            scope=scope,
            objective=objective,
            offline=offline,
        )
        event = OperationEvent(
            operation_id=operation.id,
            event_type="operation.created",
            payload={
                "name": operation.name,
                "mode": operation.mode.value,
                "scope": operation.scope.model_dump(mode="json"),
                "offline": operation.offline,
            },
        )
        self.repository.add(operation, event)
        return operation

    def get(self, operation_id: str) -> Operation:
        operation = self.repository.get(operation_id)
        if operation is None:
            raise OperationNotFoundError(f"operation not found: {operation_id}")
        return operation

    def latest(self) -> Operation | None:
        return self.repository.latest()

    def delete(self, operation_id: str) -> None:
        """Forget the operation entirely: its events, runs, artifacts, transcript."""
        if self.repository.get(operation_id) is None:
            raise OperationNotFoundError(f"operation not found: {operation_id}")
        self.repository.delete(operation_id)

    def list_all(self) -> list[Operation]:
        return self.repository.list_all()

    def transition(self, operation_id: str, target: OperationStatus) -> Operation:
        operation = self.get(operation_id)
        expected_version = operation.version
        previous = operation.transition_to(target)
        event = OperationEvent(
            operation_id=operation.id,
            event_type="operation.status.changed",
            payload={"from": previous.value, "to": target.value},
        )
        self.repository.update(operation, expected_version, event)
        return operation

    def events(self, operation_id: str) -> list[OperationEvent]:
        self.get(operation_id)
        return self.repository.events(operation_id)

    def replace_scope(self, operation_id: str, scope: ScopeContract) -> Operation:
        """Swap the scope wholesale; callers validate the contract first."""
        operation = self.get(operation_id)
        expected_version = operation.version
        operation.scope = scope
        event = OperationEvent(
            operation_id=operation.id,
            event_type="operation.scope.changed",
            payload={
                "scope": operation.scope.model_dump(mode="json"),
                "hosts": operation.scope.hosts,
            },
        )
        self.repository.update(operation, expected_version, event)
        return operation

    def rename(self, operation_id: str, name: str) -> Operation:
        """Rename the operation; the tab label follows it."""
        operation = self.get(operation_id)
        expected_version = operation.version
        previous = operation.name
        operation.name = name.strip()[:200]
        event = OperationEvent(
            operation_id=operation.id,
            event_type="operation.renamed",
            payload={"from": previous, "to": operation.name},
        )
        self.repository.update(operation, expected_version, event)
        return operation

    def set_goal(self, operation_id: str, goal: str, *, automatic: bool = False) -> Operation:
        """Set or clear the operation's completion condition.

        ``automatic`` marks a goal derived from the task rather than typed by the
        operator, so the journal can say where it came from.
        """
        operation = self.get(operation_id)
        expected_version = operation.version
        operation.goal = goal.strip()[:500]
        event = OperationEvent(
            operation_id=operation.id,
            event_type="operation.goal.changed",
            payload={"goal": operation.goal, "automatic": automatic},
        )
        self.repository.update(operation, expected_version, event)
        return operation

    def check_scope(self, operation_id: str, target: str) -> ScopeDecision:
        return self.get(operation_id).scope.evaluate(target)
