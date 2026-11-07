"""The kill-chain rail: which stage of the campaign an operation is in.

Phases are not hard-coded to a mode: they come from the operation's playbook,
with a per-mode fallback that mirrors what MTTF/PTES chains use. The counter at
the end of the rail is the mode's own measure of progress — captured flags in
CTF, passed gates in red team, verified findings in bug bounty — so a mode
without flags still reads as a campaign instead of an empty rail.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from samurai_ai.domain.graph import NodeKind, NodeState
from samurai_ai.domain.kernel import PlanTask, TaskStatus
from samurai_ai.domain.operations import OperationMode

#: Fallback chains, cheap stage first. `None` in CTF means "flags, if any".
_CHAINS: dict[OperationMode, tuple[str, ...]] = {
    OperationMode.CTF_HTB: ("recon", "foothold", "privesc", "loot"),
    OperationMode.RED_TEAM: (
        "recon",
        "initial-access",
        "execution",
        "persistence",
        "lateral",
        "impact",
    ),
    OperationMode.BUG_BOUNTY: ("recon", "surface", "exploit", "validate", "report"),
    OperationMode.OPERATION_PLAN: ("recon", "surface", "exploit", "report"),
}

#: Words that move the stage forward, with the phases each may stand for (the
#: first one the mode's chain actually has wins). Ordered: advanced stages first.
_MARKERS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("flag", "флаг", "loot", "hashdump"), ("loot", "validate")),
    (("impact", "импакт", "domain admin", "dcsync"), ("impact",)),
    (("persist", "закреп", "cron", "scheduled task", "autorun"), ("persistence",)),
    (("lateral", "латерал", "pivot", "smb exec", "wmi exec"), ("lateral",)),
    (
        ("privesc", "priv-esc", "privilege escalation", "повышение привилегий",
         "suid", "sudo", "root", "system"),
        ("privesc", "exploit"),
    ),
    (("execution", "выполнение", "rce", "reverse shell", "web shell"),
     ("execution", "exploit")),
    (("foothold", "зацеп", "shell", "session", "access"),
     ("foothold", "initial-access", "exploit")),
    (("initial access", "первичный доступ", "phish", "vpn"), ("initial-access",)),
    (("enum", "enumerat", "перечисл", "surface", "поверхность"), ("surface", "recon")),
    (("recon", "разведк", "scan", "nmap", "discovery"), ("recon",)),
)

_IMPACT_PHASE = {
    "read": "surface",
    "write": "exploit",
    "auth-bypass": "initial-access",
    "data-export": "loot",
    "rce": "privesc",
}


@dataclass(frozen=True)
class KillChain:
    """The rail to draw, its current stage and the mode's progress counter."""

    phases: tuple[str, ...]
    current: str
    counter_label: str
    counter_done: int
    counter_total: int

    def __bool__(self) -> bool:
        return bool(self.phases)


def _index_of(phase: str, phases: Sequence[str]) -> int:
    return phases.index(phase) if phase in phases else 0


def _phase_from_text(text: str, phases: Sequence[str]) -> str | None:
    lowered = text.lower()
    for markers, candidates in _MARKERS:
        if not any(marker in lowered for marker in markers):
            continue
        for candidate in candidates:
            if candidate in phases:
                return candidate
    return None


def _advanced(current: str, candidate: str | None, phases: Sequence[str]) -> str:
    if candidate is None:
        return current
    return candidate if _index_of(candidate, phases) > _index_of(current, phases) else current


def phase_for(
    mode: OperationMode,
    tasks: Iterable[PlanTask],
    *,
    fact_payloads: Iterable[dict[str, object]] = (),
    impact_levels: Iterable[str] = (),
    flag_count: int = 0,
    flag_total: int = 0,
    verified_findings: int = 0,
    gate_passed: int = 0,
    gate_total: int = 0,
    chain: Sequence[str] | None = None,
) -> KillChain:
    """Work out the rail for an operation; falsy when the mode has no chain."""
    phases = tuple(chain or _CHAINS.get(mode, ()))
    if not phases:
        return KillChain((), "", "", 0, 0)

    current = phases[0]
    for task in tasks:
        if task.status in (TaskStatus.DONE, TaskStatus.IN_PROGRESS):
            current = _advanced(current, _phase_from_text(task.title, phases), phases)
    for payload in fact_payloads:
        text = " ".join(str(value) for value in payload.values())
        current = _advanced(current, _phase_from_text(text, phases), phases)
    for level in impact_levels:
        current = _advanced(current, _IMPACT_PHASE.get(level), phases)

    # the rail is a countdown to the goal: flags, gates or verified findings
    if mode is OperationMode.CTF_HTB:
        return KillChain(phases, current, "flags", flag_count, flag_total or flag_count)
    if mode is OperationMode.RED_TEAM:
        return KillChain(phases, current, "gates", gate_passed, gate_total)
    return KillChain(phases, current, "verified", verified_findings, 0)


def nodes_to_payloads(nodes: Iterable[object]) -> list[dict[str, object]]:
    """Confirmed graph facts feed the stage, nothing else does."""
    return [
        dict(getattr(node, "payload", {}) or {})
        for node in nodes
        if getattr(node, "kind", None) is NodeKind.FACT
        and getattr(node, "state", None) is NodeState.CONFIRMED
    ]
