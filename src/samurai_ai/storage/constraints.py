from __future__ import annotations

from samurai_ai.domain.constraints import Constraint, ConstraintKind
from samurai_ai.storage.database import Database


class ConstraintRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, constraint: Constraint) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_constraints(
                    id, operation_id, task_id, kind, text, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    constraint.id,
                    constraint.operation_id,
                    constraint.task_id,
                    constraint.kind.value,
                    constraint.text,
                    constraint.created_by,
                    constraint.created_at.isoformat(),
                ),
            )

    def for_task(self, task_id: str) -> list[Constraint]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_constraints WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            ).fetchall()
        return [_constraint(row) for row in rows]

    def for_operation(self, operation_id: str) -> list[Constraint]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_constraints WHERE operation_id = ? ORDER BY created_at, id",
                (operation_id,),
            ).fetchall()
        return [_constraint(row) for row in rows]

    def get(self, constraint_id: str) -> Constraint | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_constraints WHERE id = ?", (constraint_id,)
            ).fetchone()
        return _constraint(row) if row else None

    def count(self, task_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM task_constraints WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row["total"]) if row else 0

    def remove(self, constraint_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM task_constraints WHERE id = ?", (constraint_id,)
            )
            return (cursor.rowcount or 0) > 0


def _constraint(row: object) -> Constraint:
    data = dict(row)  # type: ignore[call-overload]
    return Constraint(
        id=data["id"],
        operation_id=data["operation_id"],
        task_id=data["task_id"],
        kind=ConstraintKind(data["kind"]),
        text=data["text"],
        created_by=data["created_by"],
        created_at=data["created_at"],
    )
