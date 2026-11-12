"""Distil a finished shell run into a compact, model-facing summary.

The model drives the harness through a single ``shell_exec`` tool and types raw
bash commands, so a run's stdout is the tool's own *text* output — not the
JSON/XML the recipe argv would force through :mod:`samurai_ai.execution.manifests`.
Feeding that raw dump back to the model burns tokens and buries the signal.

These pure functions turn ``(command, stdout, stderr, exit_code)`` into a short
summary for the model while the operator keeps the full output in the TUI block
and the run record. For known tools (nmap, ffuf, nuclei, curl/http-headers,
hashcat) the summary is structured facts; for everything else it is a smartly
truncated slice with repeats and progress noise collapsed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable

__all__ = ["summarize_for_model", "detect_tool"]

#: Characters of distilled raw output handed to the model for unknown tools.
_DEFAULT_RAW_LIMIT = 1600
#: Lines kept from the head and tail of a long unknown output.
_HEAD_LINES = 30
_TAIL_LINES = 15
#: At most this many fact lines from a structured parser.
_MAX_FACTS = 60

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
#: Env assignments / wrappers to skip when sniffing the executable.
_WRAPPERS = frozenset(
    {"sudo", "env", "time", "nice", "ionice", "stdbuf", "nohup", "doas", "command"}
)


def _basename(token: str) -> str:
    """The executable name without a leading path or a `.exe` tail."""
    name = token.rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".exe") else name


def detect_tool(command: str) -> str | None:
    """The security tool a raw command drives, by its leading executable.

    Skips ``sudo``/``env`` style wrappers and ``VAR=value`` prefixes. Only the
    first stage of a pipeline is considered — that is where the tool's own
    output comes from.
    """
    first_stage = re.split(r"[|;&]", command, maxsplit=1)[0]
    try:
        tokens = first_stage.split()
    except ValueError:  # pragma: no cover - split never raises on str
        return None
    for token in tokens:
        if "=" in token and not token.startswith("-"):
            continue  # VAR=value prefix
        name = _basename(token)
        if name in _WRAPPERS:
            continue
        if name in _TOOLS:
            return name
        return None  # the executable is something we have no parser for
    return None


# --- structured parsers --------------------------------------------------
# Each returns compact fact lines, or an empty list when it cannot say
# anything confident about the output (the caller then falls back to raw
# truncation so nothing is silently swallowed).


def _parse_nmap(stdout: str, _stderr: str) -> list[str]:
    facts: list[str] = []
    host = ""
    for line in stdout.splitlines():
        line = line.strip()
        report = re.match(r"Nmap scan report for (.+)", line)
        if report:
            host = report.group(1)
            continue
        # e.g. "22/tcp open  ssh  OpenSSH 8.9p1"
        port = re.match(r"(\d+/\w+)\s+(open|open\|filtered)\s+(\S+)\s*(.*)", line)
        if port:
            where = f"{host} " if host else ""
            service = port.group(3)
            extra = port.group(4).strip()
            tail = f" {extra}" if extra else ""
            facts.append(f"{where}{port.group(1)} {port.group(2)} {service}{tail}".strip())
    if not facts:
        # nmap ran but found nothing open — that is itself the answer.
        if host and re.search(r"\bNmap done\b", stdout):
            return [f"{host}: no open ports in the scanned range"]
        if re.search(r"\bNmap done\b", stdout):
            return ["no open ports in the scanned range"]
    return facts


def _parse_ffuf(stdout: str, _stderr: str) -> list[str]:
    facts: list[str] = []
    # Native recipe forces JSON, but a raw run is usually plain text. Try JSON
    # (whole-doc or JSONL) first, then the default coloured/text table.
    stripped = _ANSI.sub("", stdout)
    for record in _iter_json_objects(stripped):
        results = record.get("results") if isinstance(record, dict) else None
        rows: Iterable[object] = results if isinstance(results, list) else [record]
        for row in rows:
            if not isinstance(row, dict):
                continue
            word = row.get("input", {})
            keyword = word.get("FUZZ") if isinstance(word, dict) else None
            path = keyword or row.get("url") or row.get("host") or "?"
            status = row.get("status")
            length = row.get("length")
            if status is not None:
                facts.append(f"{path}  [{status}]  len={length}")
    if facts:
        return facts[:_MAX_FACTS]
    for line in stripped.splitlines():
        line = line.strip()
        # "admin                   [Status: 200, Size: 1234, Words: 5, ...]"
        hit = re.match(r"(\S+)\s+\[Status:\s*(\d+),\s*Size:\s*(\d+)", line)
        if hit:
            facts.append(f"{hit.group(1)}  [{hit.group(2)}]  len={hit.group(3)}")
    return facts[:_MAX_FACTS]


def _parse_nuclei(stdout: str, _stderr: str) -> list[str]:
    facts: list[str] = []
    stripped = _ANSI.sub("", stdout)
    # JSONL first (-jsonl / -j), then the default bracketed text lines.
    for record in _iter_json_objects(stripped):
        if not isinstance(record, dict):
            continue
        template = record.get("template-id") or record.get("templateID") or "?"
        info = record.get("info", {})
        severity = info.get("severity", "?") if isinstance(info, dict) else "?"
        matched = record.get("matched-at") or record.get("host") or "?"
        facts.append(f"[{severity}] {template} @ {matched}")
    if facts:
        return facts[:_MAX_FACTS]
    for line in stripped.splitlines():
        line = line.strip()
        # "[template-id] [http] [high] https://target/path"
        hit = re.match(r"\[([^\]]+)\]\s+\[[^\]]+\]\s+\[([^\]]+)\]\s+(\S+)", line)
        if hit:
            facts.append(f"[{hit.group(2)}] {hit.group(1)} @ {hit.group(3)}")
    return facts[:_MAX_FACTS]


_HEADER_KEYS = (
    "server",
    "location",
    "content-type",
    "content-length",
    "www-authenticate",
    "set-cookie",
    "x-powered-by",
    "strict-transport-security",
)


def _parse_http_headers(stdout: str, _stderr: str) -> list[str]:
    facts: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        status = re.match(r"HTTP/[\d.]+\s+(\d{3}.*)", line)
        if status:
            facts.append(f"HTTP {status.group(1)}".strip())
            continue
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() in _HEADER_KEYS:
            # one Set-Cookie line is enough to signal a session cookie
            facts.append(f"{key.strip()}: {value.strip()[:120]}")
    return facts[:_MAX_FACTS]


def _parse_hashcat(stdout: str, _stderr: str) -> list[str]:
    facts: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.search(r"\bExhausted\b", line):
            facts.append("status: exhausted (no more candidates)")
            continue
        if re.search(r"\bCracked\b", line):
            facts.append("status: cracked")
            continue
        # recovered hash lines look like "<hash>:<plaintext>"
        if ":" in line and not line.startswith(("[", "#", "*", "Session")):
            hashed, _, plain = line.partition(":")
            if hashed and plain and " " not in hashed:
                facts.append(f"cracked {hashed[:24]}… : {plain}")
    return facts[:_MAX_FACTS]


_TOOLS: dict[str, Callable[[str, str], list[str]]] = {
    "nmap": _parse_nmap,
    "ffuf": _parse_ffuf,
    "nuclei": _parse_nuclei,
    "curl": _parse_http_headers,
    "wget": _parse_http_headers,
    "http-headers": _parse_http_headers,
    "hashcat": _parse_hashcat,
}


def _iter_json_objects(text: str) -> Iterable[object]:
    """JSON objects from a blob: a single document, then line-delimited."""
    text = text.strip()
    if not text:
        return
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# --- raw fallback --------------------------------------------------------


def _collapse(text: str) -> tuple[list[str], int]:
    """Clean noise from raw output; return (lines, collapsed_repeat_count)."""
    cleaned: list[tuple[str, int]] = []
    for raw in text.splitlines():
        # progress bars rewrite one line with \r — keep only the final frame
        line = _ANSI.sub("", raw.split("\r")[-1]).rstrip()
        if not line and cleaned and not cleaned[-1][0]:
            continue  # squeeze blank runs
        if cleaned and line == cleaned[-1][0]:
            prev_line, prev_count = cleaned[-1]
            cleaned[-1] = (prev_line, prev_count + 1)
        else:
            cleaned.append((line, 1))
    out: list[str] = []
    collapsed = 0
    for line, count in cleaned:
        if count > 1:
            collapsed += count - 1
            out.append(f"{line}  (×{count})")
        else:
            out.append(line)
    return out, collapsed


def _truncate_raw(stdout: str, stderr: str, raw_limit: int) -> str:
    lines, collapsed = _collapse(stdout)
    note = ""
    if len(lines) > _HEAD_LINES + _TAIL_LINES:
        hidden = len(lines) - _HEAD_LINES - _TAIL_LINES
        kept = lines[:_HEAD_LINES] + [f"… {hidden} lines hidden …"] + lines[-_TAIL_LINES:]
        lines = kept
    body = "\n".join(lines)
    if len(body) > raw_limit:
        body = body[:raw_limit].rstrip() + "\n… output clipped …"
    if collapsed:
        note = f"\n[{collapsed} repeated line(s) collapsed]"
    err = _stderr_tail(stderr)
    tail = f"\nstderr:\n{err}" if err else ""
    return f"{body}{note}{tail}"


def _stderr_tail(stderr: str, limit: int = 400) -> str:
    text = _ANSI.sub("", stderr).strip()
    if not text:
        return ""
    if len(text) > limit:
        text = "… " + text[-limit:]
    return text


# --- public --------------------------------------------------------------


def summarize_for_model(
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    *,
    run_id: str | None = None,
    raw_limit: int = _DEFAULT_RAW_LIMIT,
) -> str:
    """A compact, model-facing body for a finished run.

    Known tools get structured facts; others get a cleaned, head-and-tail
    slice. Either way a pointer names the run so the operator can open the full
    output in the TUI. The envelope (``[tool result …]`` header) stays with the
    caller so the prompt prefix is unchanged.
    """
    pointer = f"[full output in UI · run={run_id}]" if run_id else "[full output in UI]"
    tool = detect_tool(command)
    if tool is not None and (exit_code in (0, None)):
        facts = _TOOLS[tool](stdout, stderr)
        if facts:
            header = f"{tool}: {len(facts)} result(s)"
            err = _stderr_tail(stderr)
            tail = f"\nstderr:\n{err}" if err else ""
            return f"{header}\n" + "\n".join(facts[:_MAX_FACTS]) + f"{tail}\n{pointer}"
    # unknown tool, a failure, or a parser that found nothing → cleaned raw
    body = _truncate_raw(stdout, stderr, raw_limit)
    if not body.strip():
        body = "(no output)"
    return f"{body}\n{pointer}"
