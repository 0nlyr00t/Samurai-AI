"""Learned skills: distil a successful episode, gate it, reuse it.

The loop mirrors the other learning layers so it composes with them instead of
competing:

* **extract** reads a finished operation deterministically — the commands that
  completed, the ones that failed (pitfalls), and the findings that were
  confirmed — and writes a ``shadow`` skill with that provenance. No model call,
  so CI and scripted bench providers stay reproducible.
* **promote** is the gate: a skill reaches ``active`` only with evidence (a
  confirmed finding behind it), never on a model's say-so.
* **prompt_fragment** injects ranked ``active`` skills, and only when the mode is
  ``inject`` — labelled as agent-distilled and unverified.
* **record_reuse_outcome** accrues objective fitness from later runs, and
  **curate** retires skills that, once comparable, never help.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from samurai_ai.domain.execution import SecretRedactor, ToolRun, ToolRunStatus
from samurai_ai.domain.kernel import Finding, FindingStatus
from samurai_ai.domain.learned_skills import (
    DIGEST_MAX_SKILLS,
    PROMPT_MAX_CHARS,
    LearnedSkill,
    LearnedSkillFitness,
    LearnedSkillGateError,
    LearnedSkillKind,
    LearnedSkillMode,
    LearnedSkillNotFoundError,
    LearnedSkillStatus,
    check_promote,
    compose_body,
    compose_cue,
    compose_title,
    content_sha,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.learned_skills import LearnedSkillRepository

MODE_KEY = "learned_skills.mode"

#: a confirmed finding is the only outcome that teaches a positive skill.
CONFIRMED: frozenset[FindingStatus] = frozenset(
    {FindingStatus.VALIDATED, FindingStatus.REPORTED}
)
#: how many commands a procedure may carry before it is trimmed.
MAX_STEPS = 12
MAX_PITFALLS = 5


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LearnedSkillService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        tool_runs: ToolRunRepository,
        repo: LearnedSkillRepository,
        settings: SettingsService,
        vault: VaultService | None = None,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.tool_runs = tool_runs
        self.repo = repo
        self.settings = settings
        self.vault = vault

    # --- mode -------------------------------------------------------------
    def mode(self) -> LearnedSkillMode:
        raw = (self.settings.get_value(MODE_KEY) or "").strip()
        try:
            return LearnedSkillMode(raw) if raw else LearnedSkillMode.SHADOW
        except ValueError:
            return LearnedSkillMode.SHADOW

    def set_mode(self, mode: LearnedSkillMode) -> None:
        self.settings.set_value(MODE_KEY, mode.value)

    # --- extraction -------------------------------------------------------
    def extract(
        self, operation_id: str, workspace: str, run_id: str | None = None
    ) -> list[LearnedSkill]:
        """Distil one skill from a finished operation; idempotent by content.

        An episode with no confirmed finding teaches nothing, so nothing is
        written — a skill can never be more confident than its evidence.
        """
        self.operations.get(operation_id)
        findings = [
            finding
            for finding in self.kernel.kernel.list_findings(operation_id)
            if finding.status in CONFIRMED
        ]
        if not findings:
            return []
        redactor = self._redactor()
        completed = self.tool_runs.list_runs(operation_id, ToolRunStatus.COMPLETED)
        steps = _commands(completed, redactor, MAX_STEPS)
        pitfalls = self._pitfalls(operation_id, redactor)
        subject = _subject(findings)
        cue = compose_cue(workspace, subject)
        title = compose_title(subject)
        body = compose_body(
            steps, [redactor.redact(f.title).text for f in findings], pitfalls
        )
        sha = content_sha(workspace, cue, body)
        skill = LearnedSkill(
            workspace=workspace,
            kind=LearnedSkillKind.GENERAL,
            title=title,
            cue=cue,
            body=body,
            status=LearnedSkillStatus.SHADOW,
            source_operation_id=operation_id,
            source_run_id=run_id,
            evidence=tuple(f.id for f in findings),
            sha256=sha,
        )
        existing = self.repo.find_by_sha(workspace, sha)
        stored = self.repo.add(skill)
        if existing is None:
            self.operations.repository.append_event(
                OperationEvent(
                    operation_id=operation_id,
                    event_type="learned_skill.extracted",
                    payload={
                        "skill_id": stored.id,
                        "workspace": workspace,
                        "evidence": list(stored.evidence),
                    },
                )
            )
        return [stored]

    def _pitfalls(self, operation_id: str, redactor: SecretRedactor) -> list[str]:
        failed = self.tool_runs.list_runs(operation_id, ToolRunStatus.FAILED)
        timed_out = self.tool_runs.list_runs(operation_id, ToolRunStatus.TIMEOUT)
        return _commands(failed + timed_out, redactor, MAX_PITFALLS)

    # --- prompt -----------------------------------------------------------
    def prompt_fragment(
        self, workspace: str, task_text: str = ""
    ) -> tuple[str, tuple[str, ...]]:
        """Ranked active skills, bounded and redacted; empty unless mode is inject."""
        if self.mode() is not LearnedSkillMode.INJECT:
            return "", ()
        ranked = self._ranked_active(workspace, task_text)
        if not ranked:
            return "", ()
        redactor = self._redactor()
        parts: list[str] = []
        ids: list[str] = []
        for skill in ranked[:DIGEST_MAX_SKILLS]:
            body = skill.body
            if len(body) > PROMPT_MAX_CHARS:
                body = body[:PROMPT_MAX_CHARS] + "…"
            parts.append(
                f"### Learned skill: {skill.title}\n"
                f"When: {redactor.redact(skill.cue).text}\n"
                f"{redactor.redact(body).text}"
            )
            ids.append(skill.id)
        return "\n\n".join(parts), tuple(ids)

    def _ranked_active(self, workspace: str, task_text: str) -> list[LearnedSkill]:
        active = self.repo.list(workspace, LearnedSkillStatus.ACTIVE)
        return sorted(
            active,
            key=lambda skill: (
                skill.relevance_to(task_text),
                skill.fitness.finding_rate,
                skill.updated_at,
            ),
            reverse=True,
        )

    # --- fitness ----------------------------------------------------------
    def record_reuse_outcome(
        self, skill_ids: Sequence[str], *, produced_finding: bool, invalid_actions: int
    ) -> None:
        """Accrue objective fitness for skills that were injected into a run."""
        now = _now()
        for skill_id in skill_ids:
            skill = self.repo.get(skill_id)
            if skill is None:
                continue
            old = skill.fitness
            reuses = old.reuses + 1
            mean_invalid = (
                old.median_invalid_actions * old.reuses + max(invalid_actions, 0)
            ) / reuses
            self.repo.record_fitness(
                skill_id,
                LearnedSkillFitness(
                    reuses=reuses,
                    findings_after_reuse=old.findings_after_reuse + (1 if produced_finding else 0),
                    median_invalid_actions=mean_invalid,
                ),
                now,
            )

    # --- operator / gate --------------------------------------------------
    def get(self, skill_id: str) -> LearnedSkill:
        skill = self.repo.get(skill_id)
        if skill is None:
            raise LearnedSkillNotFoundError(f"learned skill not found: {skill_id}")
        return skill

    def all(
        self, workspace: str | None = None, status: LearnedSkillStatus | None = None
    ) -> list[LearnedSkill]:
        return self.repo.list(workspace, status)

    def promote(self, skill_id: str) -> LearnedSkill:
        skill = self.get(skill_id)
        check_promote(skill, None)
        promoted = self.repo.set_status(skill_id, LearnedSkillStatus.ACTIVE, _now())
        if promoted is None:
            raise LearnedSkillNotFoundError(f"learned skill not found: {skill_id}")
        return promoted

    def reject(self, skill_id: str) -> LearnedSkill:
        return self._set_status(skill_id, LearnedSkillStatus.RETIRED)

    def retire(self, skill_id: str) -> LearnedSkill:
        return self._set_status(skill_id, LearnedSkillStatus.RETIRED)

    def _set_status(self, skill_id: str, status: LearnedSkillStatus) -> LearnedSkill:
        updated = self.repo.set_status(skill_id, status, _now())
        if updated is None:
            raise LearnedSkillNotFoundError(f"learned skill not found: {skill_id}")
        return updated

    def curate(self, workspace: str | None = None) -> int:
        """Retire active skills that, once comparable, never led to a finding."""
        retired = 0
        for skill in self.repo.list(workspace, LearnedSkillStatus.ACTIVE):
            if skill.fitness.comparable and skill.fitness.findings_after_reuse == 0:
                self.repo.set_status(skill.id, LearnedSkillStatus.RETIRED, _now())
                retired += 1
        return retired

    # --- helpers ----------------------------------------------------------
    def _redactor(self) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            for metadata in self.vault.list_metadata(None):
                redactor.register(self.vault.get(metadata.name))
        return redactor


def _commands(runs: Sequence[ToolRun], redactor: SecretRedactor, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for run in runs:
        command = str(run.parameters.get("command", "")).strip()
        if not command:
            continue
        command = redactor.redact(command).text
        if command in seen:
            continue
        seen.add(command)
        out.append(command)
        if len(out) >= limit:
            break
    return out


def _subject(findings: list[Finding]) -> str:
    return findings[0].title if findings else ""


__all__ = [
    "CONFIRMED",
    "MODE_KEY",
    "LearnedSkillService",
    "LearnedSkillGateError",
    "LearnedSkillMode",
]
