from __future__ import annotations

from datetime import UTC, datetime

from samurai_ai.domain.access import Principal, Role, Session
from samurai_ai.storage.database import Database


class AccessRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add_session(self, session: Session) -> None:
        principal = session.principal
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO access_sessions(
                    id, subject, email, display, role, issuer,
                    access_expires_at, created_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    principal.subject,
                    principal.email,
                    principal.display,
                    principal.role.value,
                    principal.issuer,
                    session.access_expires_at.isoformat(),
                    session.created_at.isoformat(),
                    session.ended_at.isoformat() if session.ended_at else None,
                ),
            )

    def open_session(self) -> Session | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM access_sessions WHERE ended_at IS NULL "
                "ORDER BY created_at DESC"
            ).fetchone()
        return _session(row) if row else None

    def end_open_sessions(self) -> int:
        now = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE access_sessions SET ended_at = ? WHERE ended_at IS NULL", (now,)
            )
            return cursor.rowcount or 0

    def extend_session(self, session_id: str, expires_at: datetime) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE access_sessions SET access_expires_at = ? WHERE id = ?",
                (expires_at.isoformat(), session_id),
            )

    def replace_principal(self, session_id: str, principal: Principal) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE access_sessions
                SET subject = ?, email = ?, display = ?, role = ?, issuer = ?
                WHERE id = ?
                """,
                (
                    principal.subject,
                    principal.email,
                    principal.display,
                    principal.role.value,
                    principal.issuer,
                    session_id,
                ),
            )

    def list_sessions(self) -> list[Session]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM access_sessions ORDER BY created_at DESC"
            ).fetchall()
        return [_session(row) for row in rows]

    # --- role mappings ----------------------------------------------------
    def set_mapping(self, claim_value: str, role: Role) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO role_mappings(claim_value, role, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(claim_value) DO UPDATE SET role = excluded.role
                """,
                (claim_value, role.value, datetime.now(UTC).isoformat()),
            )

    def get_mapping(self, claim_value: str) -> Role | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT role FROM role_mappings WHERE claim_value = ?", (claim_value,)
            ).fetchone()
        return Role(row["role"]) if row else None

    def list_mappings(self) -> list[tuple[str, Role]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT claim_value, role FROM role_mappings ORDER BY claim_value"
            ).fetchall()
        return [(row["claim_value"], Role(row["role"])) for row in rows]

    def remove_mapping(self, claim_value: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM role_mappings WHERE claim_value = ?", (claim_value,)
            )
            return (cursor.rowcount or 0) > 0


def _session(row: object) -> Session:
    data = dict(row)  # type: ignore[call-overload]
    return Session(
        id=data["id"],
        principal=Principal(
            subject=data["subject"],
            email=data["email"],
            display=data["display"],
            role=Role(data["role"]),
            issuer=data["issuer"],
        ),
        access_expires_at=datetime.fromisoformat(data["access_expires_at"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        ended_at=datetime.fromisoformat(data["ended_at"]) if data["ended_at"] else None,
    )
