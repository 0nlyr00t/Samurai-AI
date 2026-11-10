from __future__ import annotations

import json

from samurai_ai.domain.graph import (
    ALLOWED_STATES,
    GraphConflictError,
    GraphNode,
    NodeKind,
    NodeState,
)
from samurai_ai.storage.database import Database


class GraphRepository:
    """Rows of the exploration graph.

    State moves are compare-and-set: the caller names the state it saw, and a
    lost race raises instead of overwriting the operator's decision.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, node: GraphNode) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO exploration_nodes(
                    id, operation_id, task_id, kind, state, payload, priority,
                    blocked_reason, delete_reason, created_by, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.id,
                    node.operation_id,
                    node.task_id,
                    node.kind.value,
                    node.state.value,
                    json.dumps(node.payload),
                    node.priority,
                    node.blocked_reason,
                    node.delete_reason,
                    node.created_by,
                    node.created_at.isoformat(),
                    node.updated_at.isoformat(),
                    node.completed_at.isoformat() if node.completed_at else None,
                ),
            )

    def get(self, node_id: str) -> GraphNode | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM exploration_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        return _node(row) if row else None

    def set_payload(self, node_id: str, payload: dict[str, object], *, now: str) -> None:
        """Rewrite a node's payload, keeping the title the row already had."""
        import json as _json

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE exploration_nodes SET payload = ?, updated_at = ? WHERE id = ?",
                (_json.dumps(payload, ensure_ascii=False), now, node_id),
            )

    def transition(
        self,
        node_id: str,
        expected: NodeState,
        state: NodeState,
        *,
        reason: str | None = None,
        completed: bool = False,
        now: str,
    ) -> GraphNode:
        """Move one node from ``expected`` to ``state`` or refuse."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE exploration_nodes
                SET state = ?, updated_at = ?,
                    blocked_reason = CASE WHEN ? = 'blocked' THEN ? ELSE blocked_reason END,
                    delete_reason  = CASE WHEN ? = 'deleted' THEN ? ELSE delete_reason END,
                    completed_at   = CASE WHEN ? THEN ? ELSE completed_at END
                WHERE id = ? AND state = ?
                """,
                (
                    state.value,
                    now,
                    state.value,
                    reason,
                    state.value,
                    reason,
                    int(completed),
                    now,
                    node_id,
                    expected.value,
                ),
            )
            if (cursor.rowcount or 0) == 0:
                current = connection.execute(
                    "SELECT state FROM exploration_nodes WHERE id = ?", (node_id,)
                ).fetchone()
                if current is None:
                    raise GraphConflictError(f"node {node_id} disappeared")
                raise GraphConflictError(
                    f"node {node_id} is {current['state']}, not {expected.value}"
                )
            row = connection.execute(
                "SELECT * FROM exploration_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        assert row is not None
        return _node(row)

    def dismiss(self, node_id: str, now: str) -> GraphNode | None:
        """A fact or finding dies without a state race: they are born terminal."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE exploration_nodes SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    NodeState.DISMISSED.value,
                    now,
                    node_id,
                    NodeState.CONFIRMED.value,
                ),
            )
            if (cursor.rowcount or 0) == 0:
                return None
            row = connection.execute(
                "SELECT * FROM exploration_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        return _node(row) if row else None

    def add_edge(self, operation_id: str, src: str, dst: str, rel: str, now: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO exploration_edges(
                    operation_id, src_id, dst_id, rel, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (operation_id, src, dst, rel, now),
            )

    def sources(self, node_id: str, rel: str) -> list[GraphNode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT n.* FROM exploration_edges e
                JOIN exploration_nodes n ON n.id = e.src_id
                WHERE e.dst_id = ? AND e.rel = ?
                ORDER BY n.created_at
                """,
                (node_id, rel),
            ).fetchall()
        return [_node(row) for row in rows]

    def targets(self, node_id: str, rel: str) -> list[GraphNode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT n.* FROM exploration_edges e
                JOIN exploration_nodes n ON n.id = e.dst_id
                WHERE e.src_id = ? AND e.rel = ?
                ORDER BY n.created_at
                """,
                (node_id, rel),
            ).fetchall()
        return [_node(row) for row in rows]

    def for_task(self, task_id: str) -> list[GraphNode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM exploration_nodes WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            ).fetchall()
        return [_node(row) for row in rows]

    def for_operation(self, operation_id: str) -> list[GraphNode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM exploration_nodes WHERE operation_id = ? ORDER BY created_at, id",
                (operation_id,),
            ).fetchall()
        return [_node(row) for row in rows]

    def count_intents(self, task_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM exploration_nodes WHERE task_id = ? AND kind = ?",
                (task_id, NodeKind.INTENT.value),
            ).fetchone()
        return int(row["total"]) if row else 0

    def count_open_intents(self, task_id: str) -> int:
        with self.database.connect() as connection:
            placeholders = ", ".join("?" for _ in self._open_states())
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM exploration_nodes "
                f"WHERE task_id = ? AND kind = ? AND state IN ({placeholders})",
                (task_id, NodeKind.INTENT.value, *self._open_states()),
            ).fetchone()
        return int(row["total"]) if row else 0

    def active_hints(self, task_id: str) -> list[GraphNode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM exploration_nodes "
                "WHERE task_id = ? AND kind = ? AND state = ? ORDER BY created_at",
                (task_id, NodeKind.HINT.value, NodeState.ACTIVE.value),
            ).fetchall()
        return [_node(row) for row in rows]

    def consume_hints(self, task_id: str, now: str) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE exploration_nodes SET state = ?, updated_at = ? "
                "WHERE task_id = ? AND kind = ? AND state = ?",
                (
                    NodeState.CONSUMED.value,
                    now,
                    task_id,
                    NodeKind.HINT.value,
                    NodeState.ACTIVE.value,
                ),
            )
            return int(cursor.rowcount or 0)

    @staticmethod
    def _open_states() -> tuple[str, ...]:
        return tuple(state.value for state in ALLOWED_STATES[NodeKind.INTENT])


def _node(row: object) -> GraphNode:
    data = dict(row)  # type: ignore[call-overload]
    return GraphNode(
        id=data["id"],
        operation_id=data["operation_id"],
        task_id=data["task_id"],
        kind=NodeKind(data["kind"]),
        state=NodeState(data["state"]),
        payload=json.loads(data["payload"] or "{}"),
        priority=int(data["priority"]),
        blocked_reason=data["blocked_reason"],
        delete_reason=data["delete_reason"],
        created_by=data["created_by"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        completed_at=data["completed_at"],
    )
