"""Scan tracked files and the whole history for credential-shaped strings.

The repository is public, so a leaked key is public too. Patterns are the shapes
that matter in practice; every hit is printed with its file or commit so a human
can judge it (a test fixture is fine, a live key is not).
"""

from __future__ import annotations

import re
import subprocess
import sys

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style", re.compile(r"sk-[A-Za-z0-9]{24,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("kimi-key", re.compile(r"sk-[A-Za-z0-9]{40,}")),
    ("htb-jwt", re.compile(r"eyJhbGciOiJIUzI1NiJ9?\.[A-Za-z0-9._-]{20,}")),
    ("private-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
)

#: values that are knowingly fake; a hit matching one of these is fine
ALLOW = re.compile(r"AKIA1234567890ABCDEF|sk-test-", re.IGNORECASE)


def tracked_hits() -> list[str]:
    files = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    hits: list[str] = []
    for name in files:
        try:
            text = open(name, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(0)
                if ALLOW.search(value):
                    continue
                hits.append(f"{name}: {label}: {value[:60]}")
    return hits


def history_hits() -> list[str]:
    log = subprocess.run(
        ["git", "log", "-p", "--all", "--no-color", "-U0"],
        capture_output=True, text=True, errors="ignore", check=True,
    ).stdout
    hits: list[str] = []
    seen: set[str] = set()
    for line in log.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0)
                if ALLOW.search(value) or value in seen:
                    continue
                seen.add(value)
                hits.append(f"history: {label}: {value[:60]}")
    return hits


def main() -> int:
    hits = tracked_hits() + history_hits()
    for hit in hits:
        print(hit)
    print(f"{len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
