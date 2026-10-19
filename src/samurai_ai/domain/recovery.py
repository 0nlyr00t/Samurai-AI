from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from samurai_ai.domain.kernel import OperationPlan
from samurai_ai.domain.scope import ScopeContract


class RecoveryStateError(ValueError):
    pass


class RecoveryActionStatus(StrEnum):
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RecoveryAction(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"act_{uuid4().hex[:12]}")
    operation_id: str
    task_id: str | None = None
    action_type: str = Field(min_length=1, max_length=200)
    description: str = ""
    status: RecoveryActionStatus = RecoveryActionStatus.IN_PROGRESS
    payload: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def complete(self) -> None:
        self._finish(RecoveryActionStatus.COMPLETED)

    def fail(self) -> None:
        self._finish(RecoveryActionStatus.FAILED)

    def mark_unknown(self) -> None:
        self._finish(RecoveryActionStatus.UNKNOWN)

    def _finish(self, target: RecoveryActionStatus) -> None:
        if self.status != RecoveryActionStatus.IN_PROGRESS:
            raise RecoveryStateError(
                f"recovery action {self.id} cannot move from "
                f"{self.status.value} to {target.value}"
            )
        self.status = target
        self.version += 1
        self.updated_at = datetime.now(UTC)


class OperationSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"snap_{uuid4().hex[:12]}")
    operation_id: str
    label: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecoveryReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    actions_marked_unknown: tuple[str, ...] = ()
    resumable_tasks: tuple[str, ...] = ()
    pending_approvals: tuple[str, ...] = ()
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EntityDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()


class PlanDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    tasks: EntityDiff
    gates: EntityDiff


class ScopeDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    includes: EntityDiff
    excludes: EntityDiff


def diff_plans(old: OperationPlan, new: OperationPlan) -> PlanDiff:
    old_tasks = {task.id: task.model_dump_json() for task in old.tasks}
    new_tasks = {task.id: task.model_dump_json() for task in new.tasks}
    old_gates = {gate.id: gate.model_dump_json() for gate in old.gates}
    new_gates = {gate.id: gate.model_dump_json() for gate in new.gates}
    return PlanDiff(
        tasks=_diff_entities(old_tasks, new_tasks),
        gates=_diff_entities(old_gates, new_gates),
    )


def diff_scopes(old: ScopeContract, new: ScopeContract) -> ScopeDiff:
    return ScopeDiff(
        includes=_diff_values(old.includes, new.includes),
        excludes=_diff_values(old.excludes, new.excludes),
    )


def _diff_entities(old: dict[str, str], new: dict[str, str]) -> EntityDiff:
    return EntityDiff(
        added=tuple(key for key in new if key not in old),
        removed=tuple(key for key in old if key not in new),
        changed=tuple(key for key in old if key in new and old[key] != new[key]),
    )


def _diff_values(old: tuple[str, ...], new: tuple[str, ...]) -> EntityDiff:
    old_set = set(old)
    new_set = set(new)
    return EntityDiff(
        added=tuple(value for value in new if value not in old_set),
        removed=tuple(value for value in old if value not in new_set),
    )
