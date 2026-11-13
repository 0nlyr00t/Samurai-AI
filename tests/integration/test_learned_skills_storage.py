"""Learned-skills storage: mutable rows, workspace+sha identity, migration 33."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from samurai_ai.domain.learned_skills import (
    LearnedSkill,
    LearnedSkillFitness,
    LearnedSkillStatus,
)
from samurai_ai.storage.database import Database
from samurai_ai.storage.learned_skills import LearnedSkillRepository


def _repo(tmp_path: Path) -> LearnedSkillRepository:
    return LearnedSkillRepository(Database(tmp_path / "ls.db"))


def _skill(sha: str = "a" * 64, workspace: str = "ws", **overrides: object) -> LearnedSkill:
    base = {
        "workspace": workspace,
        "title": "map smb",
        "cue": "ws: smb",
        "body": "Steps that worked:\n1. enum4linux -a host",
        "source_operation_id": "op_1",
        "evidence": ("find_1",),
        "sha256": sha,
    }
    base.update(overrides)
    return LearnedSkill(**base)  # type: ignore[arg-type]


def test_migration_creates_the_table(tmp_path: Path) -> None:
    database = Database(tmp_path / "ls.db")
    database.initialize()
    with database.connect() as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='learned_skills'"
        ).fetchone()
    assert row is not None


def test_add_then_get_round_trips(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    stored = repo.add(_skill())
    fetched = repo.get(stored.id)
    assert fetched is not None
    assert fetched.cue == "ws: smb"
    assert fetched.evidence == ("find_1",)


def test_identity_is_workspace_plus_sha(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = repo.add(_skill(evidence=("find_1",)))
    again = repo.add(_skill(evidence=("find_2",)))  # same workspace+sha
    assert again.id == first.id
    assert len(repo.list("ws")) == 1
    # a different workspace with the same sha is a different skill
    repo.add(_skill(workspace="other"))
    assert len(repo.list()) == 2


def test_list_filters_by_status(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    shadow = repo.add(_skill(sha="a" * 64))
    active = repo.add(_skill(sha="b" * 64, status=LearnedSkillStatus.ACTIVE))
    assert {s.id for s in repo.list("ws", LearnedSkillStatus.ACTIVE)} == {active.id}
    assert {s.id for s in repo.list("ws", LearnedSkillStatus.SHADOW)} == {shadow.id}


def test_set_status_and_record_fitness_mutate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    stored = repo.add(_skill())
    now = datetime.now(UTC).isoformat()

    promoted = repo.set_status(stored.id, LearnedSkillStatus.ACTIVE, now)
    assert promoted is not None and promoted.status is LearnedSkillStatus.ACTIVE

    scored = repo.record_fitness(
        stored.id,
        LearnedSkillFitness(reuses=4, findings_after_reuse=3, median_invalid_actions=0.5),
        now,
    )
    assert scored is not None
    assert scored.fitness.reuses == 4
    assert scored.fitness.findings_after_reuse == 3
    assert scored.fitness.median_invalid_actions == 0.5


def test_find_by_sha(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    stored = repo.add(_skill(sha="c" * 64))
    assert repo.find_by_sha("ws", "c" * 64) is not None
    assert repo.find_by_sha("ws", "c" * 64).id == stored.id  # type: ignore[union-attr]
    assert repo.find_by_sha("ws", "d" * 64) is None


def test_missing_row_returns_none(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    now = datetime.now(UTC).isoformat()
    assert repo.get("nope") is None
    assert repo.set_status("nope", LearnedSkillStatus.ACTIVE, now) is None
    assert repo.record_fitness("nope", LearnedSkillFitness(), now) is None
