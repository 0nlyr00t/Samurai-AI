"""Skill gaps: a miss is written only for a skill that is really absent."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.skills import SkillError, SkillNotFoundError
from samurai_ai.services.skills import SkillService
from samurai_ai.storage.database import Database
from samurai_ai.storage.skill_gaps import SkillGapRepository
from samurai_ai.storage.skills import SkillRepository


@pytest.fixture
def skills(tmp_path: Path) -> SkillService:
    database = Database(tmp_path / "gaps.db")
    return SkillService(SkillRepository(database), gaps=SkillGapRepository(database))


def test_a_missing_skill_is_recorded_and_skipped(skills: SkillService) -> None:
    skills.install("known", "body", trusted=True)

    fragment, ids = skills.prompt_fragment(["known", "invented"], "ws")

    assert "known" in fragment
    assert len(ids) == 1
    assert [(row.skill, row.workspace, row.misses) for row in skills.gaps_report("ws")] == [
        ("invented", "ws", 1)
    ]


def test_repeats_lift_the_miss_count(skills: SkillService) -> None:
    skills.install("known", "body", trusted=True)

    for _ in range(3):
        skills.prompt_fragment(["known", "invented"], "ws")

    assert skills.gaps_report("ws")[0].misses == 3


def test_a_selection_where_nothing_resolves_still_fails(skills: SkillService) -> None:
    with pytest.raises(SkillNotFoundError):
        skills.prompt_fragment(["invented"], "ws")

    assert skills.gaps_report("ws")[0].skill == "invented"


def test_too_many_skills_still_fails_before_recording(skills: SkillService) -> None:
    with pytest.raises(SkillError):
        skills.prompt_fragment(["a", "b", "c", "d", "e"], "ws")

    assert skills.gaps_report("ws") == []


def test_reports_are_workspace_scoped(skills: SkillService) -> None:
    skills.record_gap("invented", "ws-a")
    skills.record_gap("invented", "ws-b")

    assert [row.workspace for row in skills.gaps_report("ws-a")] == ["ws-a"]
    assert len(skills.gaps_report()) == 2


def test_clearing_removes_rows(skills: SkillService) -> None:
    skills.record_gap("invented", "ws")

    assert skills.clear_gaps("invented") == 1
    assert skills.gaps_report("ws") == []


def test_a_service_without_the_repository_simply_skips(tmp_path: Path) -> None:
    service = SkillService(SkillRepository(Database(tmp_path / "plain.db")))

    with pytest.raises(SkillNotFoundError):
        service.prompt_fragment(["invented"], "ws")

    assert service.gaps_report() == []
