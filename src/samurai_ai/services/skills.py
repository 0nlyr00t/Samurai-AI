"""Custom skills service: trusted, versioned prompt fragments.

Skills are operator-authored prompt content and therefore a prompt-injection
surface: installation requires an explicit trust decision, content hashes are
recorded, versions are immutable, and prompt fragments are bounded and
redacted before injection.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.skills import (
    MAX_SKILLS_PER_RUN,
    PROMPT_MAX_SKILL_CHARS,
    Skill,
    SkillError,
    SkillNotFoundError,
    SkillTrustError,
)
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.skill_gaps import SkillGap, SkillGapRepository
from samurai_ai.storage.skills import SkillRepository

MAX_SKILL_NAME_CHARS = 120
DEFAULT_WORKSPACE = "workspace"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SkillService:
    def __init__(
        self,
        skills: SkillRepository,
        vault: VaultService | None = None,
        gaps: SkillGapRepository | None = None,
    ) -> None:
        self.skills = skills
        self.vault = vault
        self.gaps = gaps

    def install(
        self, name: str, body: str, *, trusted: bool, created_by: str = "operator"
    ) -> Skill:
        if not trusted:
            raise SkillTrustError(
                "skill installation requires an explicit trust decision: "
                "review the content and re-run with --trusted "
                f"(sha256 {hashlib.sha256(body.encode('utf-8')).hexdigest()})"
            )
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        skill = Skill(
            name=name,
            version=self.skills.next_version(name),
            body=body,
            sha256=digest,
            trusted=True,
            created_by=created_by,
        )
        self.skills.add(skill)
        return skill

    def latest(self, name: str) -> Skill:
        skill = self.skills.latest(name)
        if skill is None:
            raise SkillNotFoundError(f"skill not found: {name}")
        return skill

    def list_skills(self) -> list[Skill]:
        return self.skills.list_latest()

    def list_versions(self, name: str) -> list[Skill]:
        versions = self.skills.list_versions(name)
        if not versions:
            raise SkillNotFoundError(f"skill not found: {name}")
        return versions

    def prompt_fragment(
        self, names: Sequence[str], workspace: str | None = None
    ) -> tuple[str, tuple[str, ...]]:
        """Bounded, redacted prompt fragment for the given skill names.

        A name that does not exist is recorded as a gap and skipped, so a typo or
        an invented skill cannot kill a run; a selection where nothing resolves
        still fails loudly. Returns the fragment and the ids included.
        """
        if len(names) > MAX_SKILLS_PER_RUN:
            raise SkillError(
                f"at most {MAX_SKILLS_PER_RUN} skills per run, got {len(names)}"
            )
        if len(set(names)) != len(names):
            raise SkillError("duplicate skills in run selection")
        redactor = self._redactor()
        parts: list[str] = []
        ids: list[str] = []
        missing: list[str] = []
        for name in names:
            try:
                skill = self.latest(name)
            except SkillNotFoundError:
                missing.append(name)
                self.record_gap(name, workspace)
                continue
            body = skill.body
            if len(body) > PROMPT_MAX_SKILL_CHARS:
                body = body[:PROMPT_MAX_SKILL_CHARS] + "…"
            parts.append(
                f"### Skill: {skill.name} (v{skill.version})\n"
                f"{redactor.redact(body).text}"
            )
            ids.append(skill.id)
        if not parts:
            raise SkillNotFoundError(f"no such skills: {', '.join(sorted(set(names)))}")
        return "\n\n".join(parts), tuple(ids)

    def record_gap(self, skill: str, workspace: str | None = None) -> None:
        """Remember that this skill was wanted and absent."""
        if self.gaps is None:
            return
        name = " ".join(skill.split())
        if name:
            self.gaps.record(name[:MAX_SKILL_NAME_CHARS], workspace or DEFAULT_WORKSPACE, _now())

    def gaps_report(self, workspace: str | None = None) -> list[SkillGap]:
        return self.gaps.list(workspace) if self.gaps is not None else []

    def clear_gaps(self, skill: str | None = None) -> int:
        return self.gaps.clear(skill) if self.gaps is not None else 0

    def _redactor(self) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            for metadata in self.vault.list_metadata(None):
                redactor.register(self.vault.get(metadata.name))
        return redactor
