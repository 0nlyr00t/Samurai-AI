import pytest
from pydantic import ValidationError

from samurai_ai.domain.agents import AgentRun
from samurai_ai.domain.skills import Skill, SkillError, SkillTrustError


def test_skill_valid() -> None:
    skill = Skill(
        name="web-recon",
        body="Prefer passive enumeration first.",
        sha256="ab" * 32,
        trusted=True,
    )
    assert skill.id.startswith("skill_")
    assert skill.version == 1
    assert skill.trusted


def test_skill_name_validation() -> None:
    with pytest.raises(ValidationError):
        Skill(name="Web Recon", body="x", sha256="ab" * 32)
    with pytest.raises(ValidationError):
        Skill(name="", body="x", sha256="ab" * 32)


def test_skill_body_bounds() -> None:
    with pytest.raises(ValidationError):
        Skill(name="s", body="", sha256="ab" * 32)
    with pytest.raises(ValidationError):
        Skill(name="s", body="x" * 8193, sha256="ab" * 32)


def test_skill_error_hierarchy() -> None:
    assert issubclass(SkillTrustError, SkillError)
    assert issubclass(SkillError, ValueError)


def test_agent_run_memory_and_skills_defaults() -> None:
    run = AgentRun(operation_id="op1", task_id="t1")
    assert run.workspace is None
    assert run.skills == ()


def test_agent_run_memory_and_skills_assignable() -> None:
    run = AgentRun(
        operation_id="op1",
        task_id="t1",
        workspace="acme-bb",
        skills=("web-recon", "reporting"),
    )
    assert run.workspace == "acme-bb"
    assert run.skills == ("web-recon", "reporting")
