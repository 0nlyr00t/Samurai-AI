from __future__ import annotations

from samurai_ai.domain.retest import Retest, RetestVerdict
from samurai_ai.storage.database import Database


class RetestRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, retest: Retest) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO finding_retests(
                    id, operation_id, finding_id, verdict, summary, evidence,
                    control, created_by, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retest.id,
                    retest.operation_id,
                    retest.finding_id,
                    retest.verdict.value if retest.verdict else None,
                    retest.summary,
                    retest.evidence,
                    retest.control,
                    retest.created_by,
                    retest.created_at.isoformat(),
                    retest.finished_at.isoformat() if retest.finished_at else None,
                ),
            )

    def get(self, retest_id: str) -> Retest | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM finding_retests WHERE id = ?", (retest_id,)
            ).fetchone()
        return _retest(row) if row else None

    def finish(
        self,
        retest_id: str,
        verdict: RetestVerdict,
        summary: str,
        evidence: str,
        control: str,
        now: str,
    ) -> Retest | None:
        """Fill a still-open retest; returns None when it already has a verdict."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE finding_retests
                SET verdict = ?, summary = ?, evidence = ?, control = ?, finished_at = ?
                WHERE id = ? AND verdict IS NULL
                """,
                (verdict.value, summary, evidence, control, now, retest_id),
            )
            if (cursor.rowcount or 0) == 0:
                return None
            row = connection.execute(
                "SELECT * FROM finding_retests WHERE id = ?", (retest_id,)
            ).fetchone()
        return _retest(row) if row else None

    def for_finding(self, finding_id: str) -> list[Retest]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM finding_retests WHERE finding_id = ? ORDER BY created_at",
                (finding_id,),
            ).fetchall()
        return [_retest(row) for row in rows]

    def open_for_finding(self, finding_id: str) -> Retest | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM finding_retests WHERE finding_id = ? AND verdict IS NULL "
                "ORDER BY created_at DESC",
                (finding_id,),
            ).fetchone()
        return _retest(row) if row else None


def _retest(row: object) -> Retest:
    data = dict(row)  # type: ignore[call-overload]
    return Retest(
        id=data["id"],
        operation_id=data["operation_id"],
        finding_id=data["finding_id"],
        verdict=RetestVerdict(data["verdict"]) if data["verdict"] else None,
        summary=data["summary"],
        evidence=data["evidence"],
        control=data["control"],
        created_by=data["created_by"],
        created_at=data["created_at"],
        finished_at=data["finished_at"],
    )
