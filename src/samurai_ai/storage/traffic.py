from __future__ import annotations

from samurai_ai.domain.traffic import TrafficBinding, TrafficPair, TrafficRole
from samurai_ai.storage.database import Database


class TrafficRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, pair: TrafficPair) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO traffic_pairs(
                    id, operation_id, method, url, status, request_body,
                    response_body, truncated, sha256, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair.id,
                    pair.operation_id,
                    pair.method,
                    pair.url,
                    pair.status,
                    pair.request_body,
                    pair.response_body,
                    int(pair.truncated),
                    pair.sha256,
                    pair.created_by,
                    pair.created_at.isoformat(),
                ),
            )

    def get(self, pair_id: str) -> TrafficPair | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM traffic_pairs WHERE id = ?", (pair_id,)
            ).fetchone()
        return _pair(row) if row else None

    def for_operation(self, operation_id: str) -> list[TrafficPair]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM traffic_pairs WHERE operation_id = ? ORDER BY created_at, id",
                (operation_id,),
            ).fetchall()
        return [_pair(row) for row in rows]

    def bind(
        self, finding_id: str, traffic_id: str, role: TrafficRole, note: str, now: str
    ) -> bool:
        """True when this is a new binding; re-binding keeps the first role."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO finding_traffic(
                    finding_id, traffic_id, role, note, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (finding_id, traffic_id, role.value, note, now),
            )
            return (cursor.rowcount or 0) > 0

    def unbind(self, finding_id: str, traffic_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM finding_traffic WHERE finding_id = ? AND traffic_id = ?",
                (finding_id, traffic_id),
            )
            return (cursor.rowcount or 0) > 0

    def bindings(self, finding_id: str) -> list[TrafficBinding]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM finding_traffic WHERE finding_id = ? ORDER BY created_at",
                (finding_id,),
            ).fetchall()
        return [
            TrafficBinding(
                finding_id=str(row["finding_id"]),
                traffic_id=str(row["traffic_id"]),
                role=TrafficRole(row["role"]),
                note=str(row["note"]),
            )
            for row in rows
        ]

    def count_bindings(self, finding_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM finding_traffic WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        return int(row["total"]) if row else 0


def _pair(row: object) -> TrafficPair:
    data = dict(row)  # type: ignore[call-overload]
    return TrafficPair(
        id=data["id"],
        operation_id=data["operation_id"],
        method=data["method"],
        url=data["url"],
        status=int(data["status"]),
        request_body=data["request_body"],
        response_body=data["response_body"],
        truncated=bool(data["truncated"]),
        sha256=data["sha256"],
        created_by=data["created_by"],
        created_at=data["created_at"],
    )
