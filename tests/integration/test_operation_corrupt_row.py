"""A corrupt operation row must not brick load: latest()/list_all() skip it."""

from __future__ import annotations

from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository


def _repo(tmp_path) -> tuple[OperationService, Database, OperationRepository]:  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "corrupt.db")
    repo = OperationRepository(database)
    return OperationService(repo), database, repo


def test_latest_skips_a_corrupt_row_for_the_newest_valid_one(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, database, repo = _repo(tmp_path)
    older = service.create(
        "Older", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.5",))
    )
    newer = service.create(
        "Newer", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.6",))
    )
    # clobber the newest row's data with a bare scope blob, as a botched write did
    with database.transaction() as connection:
        connection.execute(
            "UPDATE operations SET data = ? WHERE id = ?",
            ('{"includes": ["10.0.0.6"], "excludes": [], "hosts": {}}', newer.id),
        )

    latest = repo.latest()
    assert latest is not None and latest.id == older.id

    ids = {operation.id for operation in repo.list_all()}
    assert ids == {older.id}


def test_latest_is_none_when_every_row_is_corrupt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, database, repo = _repo(tmp_path)
    only = service.create(
        "Only", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.5",))
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE operations SET data = ? WHERE id = ?", ("{}", only.id)
        )

    assert repo.latest() is None
    assert repo.list_all() == []
