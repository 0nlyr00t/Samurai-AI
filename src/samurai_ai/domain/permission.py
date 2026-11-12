"""Operator permission modes and argument-level risk classification.

The mode is operator state, never model state: it lives in settings and is read
at the tool boundary, so a prompt cannot widen its own authority. Classification
is advisory only — it decides which badge to show and, in ``ask`` mode, that the
call deserves a confirmation even when a reusable approval exists.
"""

from __future__ import annotations

import re
import shlex
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
#: `dd of=/dev/sdb`, `tar -f`, etc.: a value flag that names a written file
_OF_FLAG = re.compile(r"\bof=([^\s;|&]+)")
#: commands whose non-flag arguments are files or directories they create/write
_WRITE_COMMANDS = frozenset(
    {"mkdir", "touch", "tee", "mkfifo", "mknod", "truncate", "install"}
)

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
    """Advisory flags for one command line.

    ``escape`` is different in kind from the advisory badges: it names the
    absolute paths a command writes to *outside* the trusted directory, and the
    boundary is enforced (``decide`` confines it), not merely surfaced.
    """

    sensitive: tuple[str, ...] = ()
    destructive: tuple[str, ...] = ()
    escape: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.sensitive and not self.destructive and not self.escape

    @property
    def badges(self) -> tuple[str, ...]:
        outside = tuple(f"writes {path} (outside the trusted dir)" for path in self.escape)
        return (*outside, *self.destructive, *self.sensitive)

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


def _outside_all(path: str, roots: tuple[str, ...]) -> bool:
    """True when an absolute path falls under none of the allowed roots.

    A relative path lands in the process working directory, which is itself a
    trusted root, so it is never an escape; only an absolute path can leave.
    """
    if not path.startswith("/"):
        return False
    return all(_is_outside(path, root) for root in roots if root)


def _write_targets(command: str) -> list[str]:
    """Paths a command creates or writes: redirects, output flags, write verbs.

    Static and best-effort by nature — a shell can still reach a path through a
    variable or an eval — but it catches the ordinary ways a command lands a
    file or a directory somewhere: ``> f``, ``-o f``, ``of=f``, and the create
    verbs (mkdir, touch, tee …) applied to their arguments.
    """
    targets: list[str] = []
    for regex in (_REDIRECT, _WRITE_FLAGS, _OF_FLAG):
        targets += [match.group(1) for match in regex.finditer(command)]
    for chunk in _SHELL_SEPARATORS.split(command):
        tokens = chunk.split()
        for index, token in enumerate(tokens):
            if token.rsplit("/", 1)[-1] in _WRITE_COMMANDS:
                targets += [arg for arg in tokens[index + 1 :] if not arg.startswith("-")]
    return [target.strip().strip("'\"") for target in targets if target.strip()]


def filesystem_escapes(command: str, allowed_roots: tuple[str, ...]) -> tuple[str, ...]:
    """Absolute write targets that fall outside every trusted root."""
    roots = tuple(root for root in allowed_roots if root)
    if not roots:
        return ()
    escapes = [
        path for path in _write_targets(command) if _outside_all(path, roots)
    ]
    return tuple(dict.fromkeys(escapes))[:MAX_BADGES]


def _sensitive_labels(command: str) -> list[str]:
    for path in _paths_in(command):
        if any(pattern.search(path) for pattern in _SENSITIVE_TOKENS):
            return ["credentials/key file"]
    return []


def _destructive_labels(command: str) -> list[str]:
    return list(
        dict.fromkeys(
            label for pattern, label in _DESTRUCTIVE_PATTERNS if pattern.search(command)
        )
    )


def classify_command(
    command: str,
    cwd: str | None = None,
    *,
    allowed_roots: tuple[str, ...] | None = None,
) -> CommandRisk:
    """Flag sensitive paths, destructive shapes and filesystem escapes.

    ``allowed_roots`` is the trusted filesystem boundary; when it is omitted the
    working directory (``cwd``) stands in as the only root, so an older caller
    that passes just ``cwd`` keeps getting the outside-cwd escape it used to see
    as a badge.
    """
    roots = allowed_roots if allowed_roots is not None else ((cwd,) if cwd else ())
    sensitive = _sensitive_labels(command)
    destructive = _destructive_labels(command)
    escapes = filesystem_escapes(command, roots)
    return CommandRisk(
        tuple(sensitive[:MAX_BADGES]), tuple(destructive[:MAX_BADGES]), escapes
    )


def classify_argv(
    argv: tuple[str, ...],
    cwd: str | None = None,
    *,
    allowed_roots: tuple[str, ...] | None = None,
) -> CommandRisk:
    """Classify a structured argv (no shell parsing needed)."""
    return classify_command(" ".join(argv), cwd, allowed_roots=allowed_roots)


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
    # A write outside the trusted directory is a boundary violation, not a risk
    # badge: the filesystem scope is enforced like the network scope. `ask`
    # offers it to the operator; yolo and never deny it — yolo means "don't ask
    # me about commands", not "let one write anywhere", and the operator widens
    # the boundary with /trust, not by waving a single command through. An
    # explicit allow rule above is the operator's deliberate override and wins.
    if risk.escape:
        return Decision(action="ask" if mode is PermissionMode.ASK else "deny")
    # `never` is headless: nobody can answer, so a destructive shape (rm -rf, dd,
    # mkfs, shutdown, write outside cwd) is denied rather than run blind. yolo is
    # the operator's explicit "don't ask me" — it stays silent even here, because
    # a yolo that still interrupts on every chmod -R or docker rm is not yolo.
    # `ask` falls through and prompts as usual; an explicit allow rule above wins.
    if risk.destructive and mode is PermissionMode.NEVER:
        return Decision(action="deny")
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
#: A single network probe's own transport timeout (nxc/ssh get 10 s injected)
#: fires well inside this; the window is only a backstop for a bare probe that
#: hangs on connect, so 35 s is plenty and saves ~25 s on every dead one.
TRANSPORT_WINDOW_SECONDS = 35.0
_TRANSPORT_COMMANDS = frozenset({"nxc", "ssh", "sshpass", "xfreerdp", "xfreerdp3"})
_NXC_TIMEOUT = "10"
_SSH_TIMEOUT = "8"
_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|"})
_WRAPPERS = frozenset({"sudo", "timeout", "time", "env", "command", "nice", "stdbuf"})


def _insert_timeout(command: str, executable: str, options: str) -> str:
    """Insert options after the executable, preserving the shell verbatim."""
    match = re.search(rf"(?<![\w/.-]){re.escape(executable)}(?=\s|$)", command)
    if match is None:
        return command
    return f"{command[:match.end()]} {options}{command[match.end():]}"


def _is_probe_executable(tokens: list[str], index: int) -> bool:
    """Whether a token starts a probe, rather than mentions one in prose."""
    start = max(
        (i for i, token in enumerate(tokens[:index]) if token in _COMMAND_SEPARATORS),
        default=-1,
    )
    prefix = tokens[start + 1:index]
    if not prefix:
        return True
    if prefix[0] in _WRAPPERS or prefix[-1] in _WRAPPERS:
        return True
    return tokens[index] == "ssh" and "sshpass" in prefix


def _has_option(tokens: list[str], *names: str) -> bool:
    return any(
        token in names or any(token.startswith(f"{name}=") for name in names)
        for token in tokens
    )


def _tail(tokens: list[str], index: int) -> list[str]:
    end = next(
        (i for i in range(index + 1, len(tokens)) if tokens[i] in _COMMAND_SEPARATORS),
        len(tokens),
    )
    return tokens[index + 1:end]


def _bound_nxc(tokens: list[str], index: int, command: str) -> str:
    tail = _tail(tokens, index)
    options: list[str] = []
    if not _has_option(tail, "--timeout"):
        options.extend(("--timeout", _NXC_TIMEOUT))
    if "rdp" in tail and not _has_option(tail, "--rdp-timeout"):
        options.extend(("--rdp-timeout", _NXC_TIMEOUT))
    if not options:
        return command
    return _insert_timeout(command, tokens[index], " ".join(options))


def _bound_ssh(tokens: list[str], index: int, command: str) -> str:
    tail = _tail(tokens, index)
    options: list[str] = []
    if not _has_option(tail, "ConnectTimeout"):
        options.extend(("-o", f"ConnectTimeout={_SSH_TIMEOUT}"))
    if not _has_option(tail, "ConnectionAttempts"):
        options.extend(("-o", "ConnectionAttempts=1"))
    if not options:
        return command
    return _insert_timeout(command, tokens[index], " ".join(options))


def bound_network_command(command: str) -> str:
    """Add missing per-connection timeouts to direct nxc and ssh probes.

    Explicit operator/model values always win. Shell text is edited only after
    ``shlex`` confirms an executable position, so an ``echo`` or a quoted
    ``bash -c`` payload is left alone. Existing flags and pipelines keep their
    exact spelling.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    bounded = command
    for index, token in enumerate(tokens):
        executable = token.rsplit("/", 1)[-1]
        if executable not in {"nxc", "ssh"} or not _is_probe_executable(tokens, index):
            continue
        if executable == "nxc":
            bounded = _bound_nxc(tokens, index, bounded)
        else:
            bounded = _bound_ssh(tokens, index, bounded)
    return bounded


def canonical_command(command: str) -> str:
    """Whitespace-insensitive command identity, without losing shell meaning."""
    try:
        return " ".join(shlex.split(command))
    except ValueError:
        return " ".join(command.split())


#: Builtins that ARE their segment's command but carry no attack intent: the
#: signature records that the segment happened, not the binary name.
_SIGNATURE_DROP = frozenset(
    {"cd", "echo", "export", "printf", "true", "false", "test", ":", "["}
)
#: Splits a command line into segments on shell separators, so ``;``-joined or
#: piped commands are each weighed on their own executable (fallback path only).
_SEGMENT_SPLIT = re.compile(r"\s*(?:;|&&|\|\||\||&)\s*|\s+")
#: Separator tokens the punctuation lexer emits between segments.
_SEGMENT_BOUNDARY = frozenset({";", "&&", "||", "|", "&", "&&&", "(", ")"})
#: An IPv4 literal, optionally after ``user@`` and with a trailing ``:port``.
_HOST_RE = re.compile(r"(?:^|@)(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?$")
#: Credential-bearing flags whose VALUE (not spelling) decides the attempt.
_USER_FLAGS = frozenset({"-u", "--user", "--username"})
_PASS_FLAGS = frozenset({"-p", "--password", "-P"})


def command_signature(command: str) -> str:
    """The intent of a shell command, with cosmetic variation stripped.

    Loop detection keyed on the exact text never fires on the real waste: the
    model retries the same credential against the same host with a different
    ``timeout``, an extra ``-o`` or a reordered flag, and every variant looks
    new. The signature keeps only what makes one attempt the same as another —
    the tool binaries, the target host, the username and whether a password is
    offered — so ``sshpass -p x ssh -o ConnectTimeout=8 user@h`` and
    ``timeout 30 sshpass -p x ssh user@h`` collapse to one call. Flags, ports,
    paths and ordering drop out. Heuristic by design: it only feeds a nudge, so a
    loose merge costs nothing and a loose split merely nudges less.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = [piece for piece in _SEGMENT_SPLIT.split(command) if piece]
    binaries: set[str] = set()
    hosts: set[str] = set()
    user = ""
    has_password = False
    expect_exec = True  # the next non-wrapper token opens a segment
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token in _SEGMENT_BOUNDARY:
            expect_exec = True
            continue
        bare = token.rsplit("/", 1)[-1]
        if token in _USER_FLAGS:
            if index + 1 < len(tokens):
                user = tokens[index + 1].strip("'\"").lower()
            skip_next = True
            continue
        if token in _PASS_FLAGS:
            has_password = True
            skip_next = True
            continue
        for flag in _USER_FLAGS:
            if token.startswith(f"{flag}="):
                user = token.split("=", 1)[1].strip("'\"").lower()
        host = _HOST_RE.search(token)
        if host:
            hosts.add(host.group(1))
            continue
        if expect_exec:
            if token.startswith("-") or bare.replace(".", "").isdigit():
                # a flag or a bare number (``timeout 60 nxc``): the real
                # executable is still ahead, so keep expecting it
                continue
            if bare.rstrip(":") in _WRAPPERS:
                # a pass-through wrapper (sudo/timeout/env …): weigh what it runs
                continue
            if bare not in _SIGNATURE_DROP and bare.replace(".", "").replace(
                "_", ""
            ).replace("-", "").isalnum():
                binaries.add(bare.lower())
            expect_exec = False
    parts = sorted(binaries) + sorted(f"@{host}" for host in hosts)
    if user:
        parts.append(f"u={user}")
    if has_password:
        parts.append("p=*")
    return " ".join(parts) or canonical_command(command)


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
    elif names & _TRANSPORT_COMMANDS:
        window = TRANSPORT_WINDOW_SECONDS
    elif names & _QUICK_COMMANDS:
        window = QUICK_WINDOW_SECONDS
    else:
        window = DEFAULT_WINDOW_SECONDS
    return window * (2 if relayed else 1)
