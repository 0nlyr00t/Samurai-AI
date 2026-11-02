"""Genes: the parts of the harness allowed to evolve.

A gene is a bounded, versioned piece of decision-layer text — a prompt block, a
tool policy note, a playbook hint. Genes are born in `shadow`, gather fitness from
eval reports, and only become `active` through a gate; one active gene per slot,
and the previous one stays available for rollback.

The enforcement layer never evolves this way: it is code and review.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

MAX_BODY = 2000
MIN_SAMPLES = 3
_SLOT = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


class GeneError(ValueError):
    pass


class GeneGateError(GeneError):
    pass


class GeneKind(StrEnum):
    PROMPT_BLOCK = "prompt-block"
    TOOL_POLICY = "tool-policy"
    PLAYBOOK_NOTE = "playbook-note"


class GeneStatus(StrEnum):
    SHADOW = "shadow"
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True)
class GeneFitness:
    """What the eval reports said about this gene."""

    samples: int = 0
    median_solve_rate: float = 0.0
    median_invalid_actions: float = 0.0
    median_cost_usd: float = 0.0

    @property
    def comparable(self) -> bool:
        return self.samples >= MIN_SAMPLES

    def better_than(self, other: GeneFitness | None) -> bool:
        """Strictly better on solve rate, or equal with fewer invalid actions."""
        if other is None:
            return True
        if self.median_solve_rate != other.median_solve_rate:
            return self.median_solve_rate > other.median_solve_rate
        return self.median_invalid_actions < other.median_invalid_actions


@dataclass
class Gene:
    slot: str
    kind: GeneKind
    title: str
    body: str
    id: str = field(default_factory=lambda: f"gene_{uuid4().hex[:12]}")
    version: int = 1
    status: GeneStatus = GeneStatus.SHADOW
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    fitness: GeneFitness = field(default_factory=GeneFitness)
    notes: str = ""

    def __post_init__(self) -> None:
        if not _SLOT.fullmatch(self.slot):
            raise GeneError(f"gene slot must be kebab-case: {self.slot!r}")
        if not self.title.strip():
            raise GeneError("gene needs a title")
        if not self.body.strip():
            raise GeneError("gene needs a body")
        if len(self.body) > MAX_BODY:
            raise GeneError(f"gene body is longer than {MAX_BODY}")

    @property
    def digest(self) -> str:
        return hashlib.sha256(f"{self.slot}\n{self.body}".encode()).hexdigest()

    def promote(self) -> None:
        if not self.fitness.comparable:
            raise GeneGateError(
                f"gene {self.id} has {self.fitness.samples} fitness samples, "
                f"{MIN_SAMPLES} needed"
            )
        if self.status is GeneStatus.ACTIVE:
            return
        if self.status is GeneStatus.RETIRED:
            raise GeneGateError(f"gene {self.id} is retired; create a new version")
        self.status = GeneStatus.ACTIVE

    def retire(self) -> None:
        self.status = GeneStatus.RETIRED
