"""Shared MITRE ATT&CK tactic vocabulary for red team steps and planned tests."""

from __future__ import annotations

ATTACK_TACTICS: tuple[str, ...] = (
    "reconnaissance",
    "resource-development",
    "initial-access",
    "execution",
    "persistence",
    "privilege-escalation",
    "defense-evasion",
    "credential-access",
    "discovery",
    "lateral-movement",
    "collection",
    "command-and-control",
    "exfiltration",
    "impact",
)


def is_valid_tactic(value: str) -> bool:
    return value in ATTACK_TACTICS
