"""Learned skills: procedural know-how the agent distils from its own episodes.

A learned skill is the reusable half of a *successful* operation — the sequence
of commands that worked, the pitfalls, and the check that confirmed the finding —
turned into a bounded prompt fragment for later runs. It sits beside the other
learning layers without duplicating them:

* lessons are atomic "do / don't" reflexes from a single finding verdict;
* genes are global decision-layer text gated by offline eval;
* operator skills are hand-authored, *trusted*, immutable instructions;
* a learned skill is a multi-step procedure, scoped to a workspace, **agent
  authored and therefore untrusted**.

Because the body is distilled from an operation trace (which includes target
output), it is a prompt-injection surface: skills are born in ``shadow``, carry
the provenance that admitted them, and only reach the prompt through a gate —
an outcome, never the model's opinion of its own work.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

MAX_TITLE = 160
MAX_CUE = 200
MAX_BODY = 2000
#: reuses needed before fitness is comparable (mirrors genes' MIN_SAMPLES).
MIN_REUSES = 3
#: per-skill budget inside the injected fragment.
PROMPT_MAX_CHARS = 1200
#: how many active skills the digest may inject into one run.
DIGEST_MAX_SKILLS = 3

_KEBAB = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_TOKEN = re.compile(r"[a-z0-9]+")


class LearnedSkillError(ValueError):
    pass


class LearnedSkillGateError(LearnedSkillError):
    """The skill has no evidence, or is not better than the one it would replace."""


class LearnedSkillNotFoundError(LookupError):
    pass


class LearnedSkillKind(StrEnum):
    RECON = "recon"
    EXPLOIT = "exploit"
    POST = "post"
    GENERAL = "general"


class LearnedSkillStatus(StrEnum):
    SHADOW = "shadow"
    ACTIVE = "active"
    RETIRED = "retired"


class LearnedSkillMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    INJECT = "inject"


class LearnedSkillFitness(BaseModel):
    """What later reuse proved about this skill — objective, never self-scored."""

    model_config = ConfigDict(frozen=True)

    reuses: int = Field(default=0, ge=0)
    findings_after_reuse: int = Field(default=0, ge=0)
    median_invalid_actions: float = Field(default=0.0, ge=0.0)

    @property
    def comparable(self) -> bool:
        return self.reuses >= MIN_REUSES

    @property
    def finding_rate(self) -> float:
        return self.findings_after_reuse / self.reuses if self.reuses else 0.0

    def better_than(self, other: LearnedSkillFitness | None) -> bool:
        """Strictly better on finding rate, or equal with fewer invalid actions."""
        if other is None:
            return True
        if self.finding_rate != other.finding_rate:
            return self.finding_rate > other.finding_rate
        return self.median_invalid_actions < other.median_invalid_actions


class LearnedSkill(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"lskill_{uuid4().hex[:12]}")
    workspace: str = Field(min_length=1)
    kind: LearnedSkillKind = LearnedSkillKind.GENERAL
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    cue: str = Field(min_length=1, max_length=MAX_CUE)
    body: str = Field(min_length=1, max_length=MAX_BODY)
    status: LearnedSkillStatus = LearnedSkillStatus.SHADOW
    fitness: LearnedSkillFitness = Field(default_factory=LearnedSkillFitness)
    source_operation_id: str = Field(min_length=1)
    source_run_id: str | None = None
    evidence: tuple[str, ...] = ()
    sha256: str = Field(min_length=64, max_length=64)
    trusted: bool = False
    created_by: str = "agent"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def relevance_to(self, task_text: str) -> int:
        """Shared tokens between the task and this skill's cue+title.

        A deterministic, model-free ranking signal: a skill distilled while
        mapping SMB is more relevant to an SMB task than to a web one.
        """
        wanted = _tokens(task_text)
        if not wanted:
            return 0
        have = _tokens(f"{self.cue} {self.title}")
        return len(wanted & have)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        token for token in (m.group() for m in _TOKEN.finditer(text.lower())) if len(token) > 2
    )


def validate_kind(value: str) -> LearnedSkillKind:
    try:
        return LearnedSkillKind(value)
    except ValueError as exc:
        raise LearnedSkillError(f"unknown learned-skill kind: {value!r}") from exc


def content_sha(workspace: str, cue: str, body: str) -> str:
    """Identity for dedup: same workspace + cue + body is the same skill."""
    return hashlib.sha256(f"{workspace}\n{cue}\n{body}".encode()).hexdigest()


def check_promote(skill: LearnedSkill, competitor: LearnedSkill | None) -> None:
    """The gate. Raises when the skill may not become active.

    Evidence (a confirmed finding in the source episode) is required, exactly as
    a lesson needs support before it is adopted. When another active skill would
    be displaced, the newcomer must be at least as good once both are comparable.
    """
    if skill.status is LearnedSkillStatus.RETIRED:
        raise LearnedSkillGateError(f"skill {skill.id} is retired; extract a new one")
    if not skill.evidence:
        raise LearnedSkillGateError(f"skill {skill.id} has no evidence and cannot be promoted")
    if competitor is None:
        return
    if (
        competitor.fitness.comparable
        and skill.fitness.comparable
        and not skill.fitness.better_than(competitor.fitness)
    ):
        raise LearnedSkillGateError(
            f"skill {skill.id} is not better than the active {competitor.id} "
            f"({skill.fitness.finding_rate:.2f} vs {competitor.fitness.finding_rate:.2f})"
        )


def compose_cue(workspace: str, subject: str) -> str:
    """Where the skill applies. A blank subject leaves only the workspace."""
    subject = " ".join(subject.split())
    workspace = " ".join(workspace.split())
    if not subject:
        return workspace[:MAX_CUE] or workspace
    text = f"{workspace}: {subject}" if workspace else subject
    return text[:MAX_CUE]


def compose_title(subject: str) -> str:
    subject = " ".join(subject.split())
    return (subject or "procedure")[:MAX_TITLE]


def compose_body(commands: list[str], findings: list[str], pitfalls: list[str]) -> str:
    """The reusable procedure: what worked, what to avoid, how it was confirmed.

    Deterministic — no model call — so the same episode always yields the same
    body and CI stays reproducible.
    """
    sections: list[str] = []
    steps = [cmd for cmd in (c.strip() for c in commands) if cmd]
    if steps:
        numbered = "\n".join(f"{index}. {cmd}" for index, cmd in enumerate(steps, start=1))
        sections.append(f"Steps that worked:\n{numbered}")
    clean_pitfalls = [p for p in (x.strip() for x in pitfalls) if p]
    if clean_pitfalls:
        sections.append("Pitfalls:\n" + "\n".join(f"- {p}" for p in clean_pitfalls))
    clean_findings = [f for f in (x.strip() for x in findings) if f]
    if clean_findings:
        sections.append(
            "Verification (confirmed findings):\n" + "\n".join(f"- {f}" for f in clean_findings)
        )
    body = "\n\n".join(sections).strip()
    if not body:
        body = "(no reusable steps recovered)"
    return body[:MAX_BODY]


__all__ = [
    "DIGEST_MAX_SKILLS",
    "MAX_BODY",
    "MAX_CUE",
    "MAX_TITLE",
    "MIN_REUSES",
    "PROMPT_MAX_CHARS",
    "LearnedSkill",
    "LearnedSkillError",
    "LearnedSkillFitness",
    "LearnedSkillGateError",
    "LearnedSkillKind",
    "LearnedSkillMode",
    "LearnedSkillNotFoundError",
    "LearnedSkillStatus",
    "check_promote",
    "compose_body",
    "compose_cue",
    "compose_title",
    "content_sha",
    "validate_kind",
]
