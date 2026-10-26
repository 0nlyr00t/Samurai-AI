"""Custom skills service: trusted, versioned prompt fragments.

Skills are operator-authored prompt content and therefore a prompt-injection
surface: installation requires an explicit trust decision, content hashes are
recorded, versions are immutable, and prompt fragments are bounded and
redacted before injection.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

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
from samurai_ai.storage.skills import SkillRepository


class SkillService:
    def __init__(self, skills: SkillRepository, vault: VaultService | None = None) -> None:
        self.skills = skills
        self.vault = vault

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

    def prompt_fragment(self, names: Sequence[str]) -> tuple[str, tuple[str, ...]]:
        """Bounded, redacted prompt fragment for the given skill names.

        Returns the fragment text and the ids of the skills included (for the
        run-start audit record).
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
        for name in names:
            skill = self.latest(name)
            body = skill.body
            if len(body) > PROMPT_MAX_SKILL_CHARS:
                body = body[:PROMPT_MAX_SKILL_CHARS] + "…"
            parts.append(
                f"### Skill: {skill.name} (v{skill.version})\n"
                f"{redactor.redact(body).text}"
            )
            ids.append(skill.id)
        return "\n\n".join(parts), tuple(ids)

    def _redactor(self) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            for metadata in self.vault.list_metadata(None):
                redactor.register(self.vault.get(metadata.name))
        return redactor
