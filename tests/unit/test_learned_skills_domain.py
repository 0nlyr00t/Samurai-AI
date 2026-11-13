"""Learned-skills domain: the gate, the fitness order, deterministic composers."""

from __future__ import annotations

import pytest

from samurai_ai.domain.learned_skills import (
    MIN_REUSES,
    LearnedSkill,
    LearnedSkillFitness,
    LearnedSkillGateError,
    LearnedSkillStatus,
    check_promote,
    compose_body,
    compose_cue,
    compose_title,
    content_sha,
)


def _skill(**overrides: object) -> LearnedSkill:
    base = {
        "workspace": "ws",
        "title": "map smb shares",
        "cue": "ws: smb enumeration",
        "body": "Steps that worked:\n1. enum4linux -a host",
        "source_operation_id": "op_1",
        "evidence": ("find_1",),
        "sha256": "a" * 64,
    }
    base.update(overrides)
    return LearnedSkill(**base)  # type: ignore[arg-type]


def test_fitness_comparable_needs_min_reuses() -> None:
    assert not LearnedSkillFitness(reuses=MIN_REUSES - 1).comparable
    assert LearnedSkillFitness(reuses=MIN_REUSES).comparable


def test_better_than_prefers_higher_finding_rate_then_fewer_invalids() -> None:
    strong = LearnedSkillFitness(reuses=4, findings_after_reuse=3)
    weak = LearnedSkillFitness(reuses=4, findings_after_reuse=1)
    assert strong.better_than(weak)
    assert not weak.better_than(strong)
    assert strong.better_than(None)

    tie_clean = LearnedSkillFitness(reuses=4, findings_after_reuse=2, median_invalid_actions=0.5)
    tie_noisy = LearnedSkillFitness(reuses=4, findings_after_reuse=2, median_invalid_actions=3.0)
    assert tie_clean.better_than(tie_noisy)


def test_promote_requires_evidence() -> None:
    with pytest.raises(LearnedSkillGateError):
        check_promote(_skill(evidence=()), None)
    check_promote(_skill(), None)  # evidence present → no raise


def test_promote_refuses_a_retired_skill() -> None:
    with pytest.raises(LearnedSkillGateError):
        check_promote(_skill(status=LearnedSkillStatus.RETIRED), None)


def test_promote_refuses_a_worse_newcomer_once_both_are_comparable() -> None:
    incumbent = _skill(
        fitness=LearnedSkillFitness(reuses=5, findings_after_reuse=5), sha256="b" * 64
    )
    newcomer = _skill(fitness=LearnedSkillFitness(reuses=5, findings_after_reuse=1))
    with pytest.raises(LearnedSkillGateError):
        check_promote(newcomer, incumbent)


def test_compose_cue_scopes_to_workspace() -> None:
    assert compose_cue("ws", "smb shares").startswith("ws: ")
    assert compose_cue("ws", "   ") == "ws"


def test_compose_body_sections_and_is_deterministic() -> None:
    body = compose_body(["nmap host", "enum4linux host"], ["IDOR on /user"], ["slow scan hangs"])
    assert "Steps that worked:" in body
    assert "1. nmap host" in body
    assert "Pitfalls:" in body
    assert "Verification" in body
    assert compose_body(["nmap host"], [], []) == compose_body(["nmap host"], [], [])


def test_empty_episode_body_is_marked() -> None:
    assert compose_body([], [], []) == "(no reusable steps recovered)"


def test_content_sha_is_stable_and_identity_bearing() -> None:
    assert content_sha("ws", "cue", "body") == content_sha("ws", "cue", "body")
    assert content_sha("ws", "cue", "body") != content_sha("ws", "cue", "other")


def test_relevance_counts_shared_tokens() -> None:
    skill = _skill(cue="ws: smb enumeration", title="map smb shares")
    assert skill.relevance_to("enumerate smb shares on the host") >= 2
    assert skill.relevance_to("web login bypass") == 0


def test_title_and_body_bounds_are_enforced() -> None:
    assert len(compose_title("x" * 500)) <= 160
    assert len(compose_body(["y" * 5000], [], [])) <= 2000
