"""Operation-scoped role floors: the owner may tighten a command inside one run."""

from __future__ import annotations

from samurai_ai.domain.access import Role
from samurai_ai.storage.database import Database


class OperationPolicyRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def effective_paths(self, operation_id: str) -> dict[str, Role]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT path, role FROM operation_role_policies WHERE operation_id = ?",
                (operation_id,),
            ).fetchall()
        return {str(row["path"]): Role(row["role"]) for row in rows}

    def set(self, operation_id: str, path: str, role: Role) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO operation_role_policies (operation_id, path, role) "
                "VALUES (?, ?, ?) ON CONFLICT(operation_id, path) DO UPDATE "
                "SET role = excluded.role",
                (operation_id, path, role.value),
            )

    def remove(self, operation_id: str, path: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM operation_role_policies WHERE operation_id = ? AND path = ?",
                (operation_id, path),
            )
        return cursor.rowcount > 0

    def role_for(self, operation_id: str, path: str) -> Role | None:
        """Exact path first, then the command group, then nothing."""
        policy = self.effective_paths(operation_id)
        if path in policy:
            return policy[path]
        return policy.get(path.split(".", 1)[0])


__all__ = ["OperationPolicyRepository"]
