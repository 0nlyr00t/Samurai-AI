"""Operator permission modes and argument-level risk classification.

The mode is operator state, never model state: it lives in settings and is read
at the tool boundary, so a prompt cannot widen its own authority. Classification
is advisory only — it decides which badge to show and, in ``ask`` mode, that the
call deserves a confirmation even when a reusable approval exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

_MODE_ORDER = ("ask", "yolo", "never")

# Credentials and key material. Matching is on the whole token, so ``.env.local``
# and ``~/.ssh/id_ed25519`` are caught while ``environment.md`` is not.
_SENSITIVE_TOKENS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|/)\.env(?:\.[\w-]+)?$"),
    re.compile(r"(?:^|/)\.env$"),
    re.compile(r"(?:^|/)\.ssh(?:/|$)"),
    re.compile(r"(?:^|/)\.aws(?:/|$)"),
    re.compile(r"(?:^|/)\.gnupg(?:/|$)"),
    re.compile(r"(?:^|/)\.docker/config\.json$"),
    re.compile(r"(?:^|/)\.netrc$"),
    re.compile(r"(?:^|/)\.npmrc$"),
    re.compile(r"(?:^|/)\.pypirc$"),
    re.compile(r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$"),
    re.compile(r"\.(?:pem|key|p12|pfx|jks|keystore)$"),
    re.compile(r"(?:^|/)(?:credentials|secrets?)(?:\.[\w-]+)?$"),
    re.compile(r"(?:^|/)shadow$"),
    re.compile(r"(?:^|/)\.kube/config$"),
)

_DESTRUCTIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+(?:-[\w-]*[rR][\w-]*|--recursive)"), "recursive delete"),
    (re.compile(r"\b(?:mkfs|mke2fs|wipefs)\b"), "filesystem create"),
    (re.compile(r"\bdd\b"), "raw disk write"),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b"), "host power"),
    (re.compile(r"\b(?:truncate|shred)\b"), "truncate/shred"),
    (re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-\w*[fd])"), "git discard"),
    (re.compile(r"\bdocker\s+(?:rm|volume\s+rm|system\s+prune)\b"), "container/volume delete"),
    (re.compile(r"\b(?:iptables|nft)\b.*(?:^|\s)-F\b"), "firewall flush"),
    (re.compile(r"\bkill\s+-9\b"), "force kill"),
    (re.compile(r"\bdrop\s+(?:table|database)\b", re.IGNORECASE), "schema drop"),
    (re.compile(r">\s*/dev/(?:sd|disk|nvme)"), "block device write"),
    (re.compile(r"\bch(?:own|mod)\s+-R\b"), "recursive permission change"),
)

_REDIRECT = re.compile(r">>?\s*([^\s;|&]+)")
_WRITE_FLAGS = re.compile(r"(?:^|\s)(?:-[oO]\w*|--output|--out)(?:\s+|=)([^\s;|&]+)")

_SHELL_SEPARATORS = re.compile(r"[;&|]{1,2}")
_TOKEN = re.compile(r"[^\s;&|<>\"']+")
MAX_BADGES = 4


class PermissionMode(StrEnum):
    ASK = "ask"
    YOLO = "yolo"
    NEVER = "never"

    @property
    def prompts_for_tools(self) -> bool:
        return self is PermissionMode.ASK

    @property
    def allows_operator_questions(self) -> bool:
        return self is not PermissionMode.NEVER


def parse_mode(value: str | None, default: PermissionMode = PermissionMode.ASK) -> PermissionMode:
    if value is None:
        return default
    try:
        return PermissionMode(value.strip().lower())
    except ValueError:
        return default


def next_mode(mode: PermissionMode) -> PermissionMode:
    """Cycle ask → yolo → never → ask (the Shift-Tab order)."""
    index = _MODE_ORDER.index(mode.value)
    return PermissionMode(_MODE_ORDER[(index + 1) % len(_MODE_ORDER)])


@dataclass(frozen=True)
class CommandRisk:
    """Advisory flags for one command line."""

    sensitive: tuple[str, ...] = ()
    destructive: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.sensitive and not self.destructive

    @property
    def badges(self) -> tuple[str, ...]:
        return (*self.destructive, *self.sensitive)

    def label(self) -> str:
        return ", ".join(self.badges)


def _paths_in(command: str) -> list[str]:
    paths: list[str] = []
    for separator_chunk in _SHELL_SEPARATORS.split(command):
        for token in _TOKEN.findall(separator_chunk):
            candidate = token.rstrip(",;")
            if candidate:
                paths.append(candidate)
    return paths


def _is_outside(path: str, cwd: str) -> bool:
    if not path.startswith("/"):
        return False
    try:
        return not PurePosixPath(path).is_relative_to(PurePosixPath(cwd))
    except (ValueError, OSError):
        return True


def classify_command(command: str, cwd: str | None = None) -> CommandRisk:
    """Flag sensitive paths and destructive shapes in a shell command line."""
    sensitive: list[str] = []
    destructive: list[str] = []

    for pattern, label in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command) and label not in destructive:
            destructive.append(label)

    for path in _paths_in(command):
        for pattern in _SENSITIVE_TOKENS:
            if pattern.search(path):
                if "credentials/key file" not in sensitive:
                    sensitive.append("credentials/key file")
                break

    if cwd is not None:
        outside = [
            match.group(1)
            for regex in (_REDIRECT, _WRITE_FLAGS)
            for match in regex.finditer(command)
            if _is_outside(match.group(1), cwd)
        ]
        if outside:
            destructive.append("write outside cwd")

    return CommandRisk(tuple(sensitive[:MAX_BADGES]), tuple(destructive[:MAX_BADGES]))


def classify_argv(argv: tuple[str, ...], cwd: str | None = None) -> CommandRisk:
    """Classify a structured argv (no shell parsing needed)."""
    return classify_command(" ".join(argv), cwd)
