"""The kill-chain rail: which stage of the campaign an operation is in.

Phases are not hard-coded to a mode: they come from the operation's playbook,
with a per-mode fallback that mirrors what MTTF/PTES chains use. The counter at
the end of the rail is the mode's own measure of progress — captured flags in
CTF, passed gates in red team, verified findings in bug bounty — so a mode
without flags still reads as a campaign instead of an empty rail.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from samurai_ai.domain.graph import NodeKind, NodeState
from samurai_ai.domain.kernel import PlanTask, TaskStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract

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
    # active exploitation attempts move the rail past recon even before a shell:
    # «собираю SSTI в заметках» is not reconnaissance any more
    (("ssti", "sql inject", "инъекц", "template inject", "forg", "поддел",
      "bypass", "обход", "payload", "пейлоад", "exploit", "экспл"),
     ("exploit", "foothold")),
    (("login", "log in", "залогин", "регистрац", "registered", "account",
      "checkout", "чекаут", "booked", "бронь"),
     ("surface", "recon")),
    (("foothold", "зацеп", "shell", "session", "access"),
     ("foothold", "initial-access", "exploit")),
    (("initial access", "первичный доступ", "phish", "vpn"), ("initial-access",)),
    (("enum", "enumerat", "перечисл", "surface", "поверхность"), ("surface", "recon")),
    (("recon", "разведк", "scan", "nmap", "discovery"), ("recon",)),
    (("report", "отчёт", "otchet", "debrief", "export"), ("report",)),
)

_IMPACT_PHASE = {
    "read": "surface",
    "write": "exploit",
    "auth-bypass": "initial-access",
    "data-export": "loot",
    "rce": "privesc",
}


class PhaseState(StrEnum):
    """Where one phase stands, always with a reason behind it."""

    NOT_STARTED = "not-started"
    ACTIVE = "active"
    BLOCKED = "blocked"
    READY = "ready-to-advance"
    COMPLETE = "complete"


@dataclass(frozen=True)
class PhaseView:
    """One phase with its exit criteria and what is still missing for them."""

    phase: str
    state: PhaseState
    goal: str = ""
    missing: tuple[str, ...] = ()
    reason: str = ""

    @property
    def marker(self) -> str:
        return {
            PhaseState.NOT_STARTED: "○",
            PhaseState.ACTIVE: "▶",
            PhaseState.BLOCKED: "✖",
            PhaseState.READY: "▸",
            PhaseState.COMPLETE: "✔",
        }[self.state]


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


def chains_for(mode: OperationMode) -> tuple[str, ...]:
    """The mode's own phase chain, for messages and validation."""
    return _CHAINS.get(mode, ())


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
    intent_texts: Iterable[str] = (),
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
    for text in intent_texts:
        # what the agent is actively testing counts too: the chat path writes
        # its work as graph intents, not plan tasks
        current = _advanced(current, _phase_from_text(text, phases), phases)
    for level in impact_levels:
        current = _advanced(current, _IMPACT_PHASE.get(level), phases)

    # the rail is a countdown to the goal: flags, gates or verified findings
    if mode is OperationMode.CTF_HTB:
        return KillChain(phases, current, "flags", flag_count, flag_total or flag_count)
    if mode is OperationMode.RED_TEAM:
        return KillChain(phases, current, "gates", gate_passed, gate_total)
    return KillChain(phases, current, "verified", verified_findings, 0)


def phase_views(
    chain: KillChain,
    tasks: Iterable[PlanTask],
    *,
    gates: Iterable[tuple[str, str]] = (),
    artifacts: Iterable[str] = (),
) -> tuple[PhaseView, ...]:
    """Per phase: state, its own goal, and the unmet exit criteria.

    A phase is complete once it is behind the rail; the current one is active
    until nothing is left undone — then it is ready to advance. Anything the
    plan already knows is blocked (a blocked task, a failed gate, a required
    artifact that never arrived) turns it into *blocked* with that named. The
    phase comes from the domain, never from a claim in the text.
    """
    if not chain.phases:
        return ()
    phase_tasks: dict[str, list[PlanTask]] = {phase: [] for phase in chain.phases}
    for task in tasks:
        phase = _phase_from_text(task.title, chain.phases)
        if phase is None:
            continue
        phase_tasks[phase].append(task)
    have_artifacts = {name.lower() for name in artifacts}
    failed_gates = {
        phase: [name for name, status in gates if status == "failed"]
        for phase in chain.phases
    }
    current_index = _index_of(chain.current, chain.phases)
    views: list[PhaseView] = []
    for index, phase in enumerate(chain.phases):
        own = phase_tasks[phase]
        goal = own[0].title[:60] if own else ""
        missing: list[str] = []
        reason = ""
        if index < current_index:
            views.append(PhaseView(phase, PhaseState.COMPLETE, goal))
            continue
        if index > current_index:
            views.append(PhaseView(phase, PhaseState.NOT_STARTED, goal))
            continue
        blocked = [task.title for task in own if task.status is TaskStatus.BLOCKED]
        for task in own:
            missing.extend(
                name
                for name in task.required_artifacts
                if name.lower() not in have_artifacts
            )
        unfinished = [
            task.title for task in own if task.status not in (TaskStatus.DONE, TaskStatus.SKIPPED)
        ]
        if failed_gates[phase]:
            reason = failed_gates[phase][0]
        if blocked:
            reason = reason or blocked[0]
        if reason:
            views.append(PhaseView(phase, PhaseState.BLOCKED, goal, tuple(missing), reason))
            continue
        if unfinished:
            views.append(PhaseView(phase, PhaseState.ACTIVE, goal, tuple(missing)))
            continue
        if missing:
            views.append(PhaseView(phase, PhaseState.BLOCKED, goal, tuple(missing), missing[0]))
            continue
        views.append(PhaseView(phase, PhaseState.READY, goal))
    return tuple(views)


def nodes_to_payloads(nodes: Iterable[object]) -> list[dict[str, object]]:
    """Confirmed graph facts feed the stage, nothing else does."""
    return [
        dict(getattr(node, "payload", {}) or {})
        for node in nodes
        if getattr(node, "kind", None) is NodeKind.FACT
        and getattr(node, "state", None) is NodeState.CONFIRMED
    ]


#: Anything that looks like a host worth naming in the side block.
_HOST_RE = re.compile(
    r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9][a-z0-9.-]*\.(?:htb|local|lan|internal|com|net|io|org))\b",
    re.IGNORECASE,
)

#: What level of access a fact reports, strongest last.
_LEVEL_RE = (
    ("root", ("root", "uid=0", "system", "domain admin", "administrator")),
    ("user", ("user", "uid=", "shell", "ssh", "www-data", "session")),
    ("read", ("read", "listing", "banner", "enum")),
)


@dataclass(frozen=True)
class AccessRow:
    host: str
    level: str
    note: str
    #: the confirmed fact this row came from — the evidence behind it
    node_id: str = ""


@dataclass(frozen=True)
class LootEntry:
    """One loot row: domain identity, what it is, where it came from."""

    kind: str
    label: str
    #: domain id — a fact node, a run, an artifact; "" when there is none
    source_id: str = ""
    at: str = ""
    level: str = ""
    used: bool = False
    secret: bool = False
    note: str = ""


def access_rows(
    facts: Iterable[tuple[str, str]] | Iterable[str], scope: ScopeContract | None = None
) -> list[AccessRow]:
    """Hosts named by confirmed facts, with the strongest level each reached.

    A fact naming two hosts gets classified per host: the level markers count
    only in the fragment about that host (up to the next host name), so
    "root on A; shell on B" does not paint both as root. With a scope given,
    out-of-scope mentions (a VPN gateway, a DNS resolver, a network base
    address quoted by a fact) are not access and do not appear.
    """
    found: dict[str, AccessRow] = {}
    order = {"read": 0, "user": 1, "root": 2}
    for entry in facts:
        text, node_id = entry if isinstance(entry, tuple) else (entry, "")
        matches = list(_HOST_RE.finditer(text))
        if not matches:
            continue
        for index, match in enumerate(matches):
            host = match.group(0)
            if scope is not None and not scope.evaluate(host).allowed:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            fragment = text[max(0, match.start() - 24):end].lower()
            level = next(
                (name for name, markers in _LEVEL_RE
                 if any(m in fragment for m in markers)),
                "read",
            )
            note = text[max(0, match.start() - 24):end].strip()[:80]
            previous = found.get(host)
            if previous is None or order[level] > order[previous.level]:
                found[host] = AccessRow(host, level, note, node_id)
    return list(found.values())


def attack_map(rows: Iterable[AccessRow]) -> list[str]:
    """An ASCII map of the access graph: host → level → finding → pivot."""
    lines: list[str] = []
    for row in rows:
        icon = {"root": "☠", "user": "●", "read": "○"}.get(row.level, "○")
        lines.append(f"{icon} {row.host}")
        if row.note:
            lines.append(f"   └─ {row.level} · {row.note}")
    return lines


def hosts_in(texts: Iterable[str]) -> set[str]:
    """Every host named anywhere in the texts, lower-cased."""
    found: set[str] = set()
    for text in texts:
        for match in _HOST_RE.finditer(text):
            found.add(match.group(0).lower())
    return found


def hosts_mentioned(text: str) -> list[str]:
    """Hosts in first-mention order — for presenting a task's targets."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _HOST_RE.finditer(text):
        host = match.group(0).lower()
        if host not in seen:
            seen.add(host)
            found.append(host)
    return found


@dataclass(frozen=True)
class ScopeRow:
    """One scope target in the side block: what it is and whether work reached it."""

    rule: str
    excluded: bool
    hit: bool
    note: str = ""


def scope_rows(scope: ScopeContract, touched: Iterable[str]) -> list[ScopeRow]:
    """The task's targets: include rules, host aliases, then exclusions.

    A rule counts as hit when a touched host evaluates against it; an alias
    counts when either its name or the address it stands for was touched, and
    a touched alias also marks the rule its address falls under.
    """
    hosts = {host.lower() for host in touched}
    matched: set[str] = set()
    for host in hosts:
        decision = scope.evaluate(scope.resolve_host(host))
        if decision.allowed and decision.matched_rule:
            matched.add(decision.matched_rule)
    rows: list[ScopeRow] = []
    excluded_rules = set(scope.excludes)
    for rule in scope.includes:
        if rule in excluded_rules:
            continue
        rows.append(ScopeRow(rule=rule, excluded=False, hit=rule in matched))
    for name, target in scope.hosts.items():
        if name in excluded_rules:
            continue
        hit = name.lower() in hosts or target.lower() in hosts
        rows.append(ScopeRow(rule=name, excluded=False, hit=hit, note=f"→ {target}"))
    # an excluded target is not an active target: it disappears from TARGETS
    # and the Scope tab rather than being shown with a misleading hit marker
    return rows
