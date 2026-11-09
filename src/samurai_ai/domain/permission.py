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


RULE_ACTIONS = ("allow", "ask", "deny")
MAX_RULES = 50
MAX_RULE_PATTERN = 200
_RULE_TOOL = re.compile(r"(?:\*|[a-z0-9][a-z0-9.-]{0,63})\Z")


class PermissionRuleError(ValueError):
    pass


@dataclass(frozen=True)
class PermissionRule:
    """One operator rule: an action bound to a tool and a command pattern.

    The pattern is matched against the command text with ``*`` as "anything",
    so ``git *`` covers ``git push --force`` but not ``gitk``.
    """

    action: str
    tool_id: str
    pattern: str

    def __post_init__(self) -> None:
        if self.action not in RULE_ACTIONS:
            raise PermissionRuleError(
                f"rule action must be one of {', '.join(RULE_ACTIONS)}: {self.action!r}"
            )
        if not _RULE_TOOL.fullmatch(self.tool_id):
            raise PermissionRuleError(f"invalid rule tool id: {self.tool_id!r}")
        if not self.pattern.strip():
            raise PermissionRuleError("rule pattern must not be empty")
        if len(self.pattern) > MAX_RULE_PATTERN:
            raise PermissionRuleError(
                f"rule pattern is longer than {MAX_RULE_PATTERN}: {self.pattern!r}"
            )

    def matches(self, tool_id: str, command: str) -> bool:
        if self.tool_id not in ("*", tool_id):
            return False
        expression = ".*".join(re.escape(part) for part in self.pattern.split("*"))
        return re.fullmatch(expression, command) is not None

    def format(self) -> str:
        return f"{self.action} {self.tool_id}: {self.pattern}"

    @classmethod
    def parse(cls, text: str) -> PermissionRule:
        action, _, rest = text.strip().partition(" ")
        tool_id, _, pattern = rest.partition(":")
        if not action or not tool_id or not pattern.strip():
            raise PermissionRuleError(
                f"rule must look like 'allow shell.exec: git *': {text!r}"
            )
        return cls(action=action, tool_id=tool_id.strip(), pattern=pattern.strip())


@dataclass(frozen=True)
class Decision:
    """What the tool boundary should do, and which rule decided it."""

    action: str
    rule: PermissionRule | None = None

    @property
    def is_allowed(self) -> bool:
        return self.action == "allow"

    @property
    def is_denied(self) -> bool:
        return self.action == "deny"


def decide(
    rules: tuple[PermissionRule, ...],
    tool_id: str,
    command: str,
    mode: PermissionMode,
    risk: CommandRisk,
    *,
    reusable: bool = False,
) -> Decision:
    """Order: deny → ask → allow → risk badge → mode.

    ``deny`` and ``ask`` are operator guards and outrank ``allow``; ``allow``
    outranks the advisory badge and the mode. Without a matching rule the
    behaviour is exactly the old one: a badge or the mode decides.
    """
    for action in ("deny", "ask", "allow"):
        for rule in rules:
            if rule.action == action and rule.matches(tool_id, command):
                return Decision(action=action, rule=rule)
    # No rule: the old behaviour, unchanged — a reusable approval keeps quiet
    # unless the command carries a badge, and only `ask` mode prompts at all.
    if not mode.prompts_for_tools:
        return Decision(action="silent")
    if reusable and risk.empty:
        return Decision(action="silent")
    return Decision(action="ask")


#: Families that finish in seconds: a resolver answer, a banner, a page.
_QUICK_COMMANDS = frozenset(
    {"curl", "dig", "host", "nslookup", "whois", "ping", "nc", "ncat", "wget", "openssl"}
)
#: Families that legitimately run for minutes: port sweeps, brute force, enumeration.
_LONG_COMMANDS = frozenset(
    {
        "nmap", "masscan", "rustscan", "ffuf", "feroxbuster", "gobuster", "dirb",
        "dirsearch", "wfuzz", "nikto", "hydra", "medusa", "sqlmap", "nuclei",
        "subfinder", "amass", "testssl.sh", "enum4linux",
    }
)
#: Short leash for quick families, long for sweeps; a relayed run doubles both
#: because the far side of a VPN answers on its own schedule.
DEFAULT_WINDOW_SECONDS = 120.0
QUICK_WINDOW_SECONDS = 25.0
LONG_WINDOW_SECONDS = 900.0


def command_window(command: str, *, relayed: bool = False) -> float:
    """How long to give a command before it is a timeout rather than work.

    A flat 120 s killed every `nmap -p-` after two minutes of real scanning (six
    such timeouts in one live run) while making a `curl` wait two minutes for a
    dead port. The family decides, not one number for everything.
    """
    # every token, not just the first: «cd /tmp && ffuf …» is still a sweep
    names = {
        token.rsplit("/", 1)[-1].lower()
        for token in command.replace(";", " ").split()
        if token and not token.startswith("-")
    }
    if names & _LONG_COMMANDS:
        window = LONG_WINDOW_SECONDS
    elif names & _QUICK_COMMANDS:
        window = QUICK_WINDOW_SECONDS
    else:
        window = DEFAULT_WINDOW_SECONDS
    return window * (2 if relayed else 1)
