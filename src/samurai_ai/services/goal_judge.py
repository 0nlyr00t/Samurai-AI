"""Judge whether an operation's completion condition already holds.

The counter keeps a run moving, but it cannot tell "still working" from
"finished". A cheap model reads the goal and a digest of what happened and
answers CONTINUE or STOP; when the judge is unavailable the caller falls back
to the counter, so a broken judge never stalls a run.
"""

from __future__ import annotations

import re

CONTINUE = "continue"
STOP = "stop"
#: how much of the turn the judge reads — the tail is what matters
DIGEST_LIMIT = 4000

_VERDICT = re.compile(r"\b(continue|stop)\b", re.IGNORECASE)

PROMPT = (
    "You judge a security operation. Given the completion condition and a "
    "digest of the latest turn, answer exactly one word: STOP only when the "
    "digest shows concrete proof the condition holds (the flag text, the shell, "
    "the artefact); announcements of intent (\"reading\", \"scanning\", \"next I "
    "will\") are not proof. When in doubt, answer CONTINUE. No prose."
)


def digest(lines: list[str], limit: int = DIGEST_LIMIT) -> str:
    """The tail of the turn, newest last, cut to the limit."""
    text = "\n".join(line for line in lines if line.strip())
    return text[-limit:]


def parse_verdict(answer: str) -> str | None:
    """Read the judge's answer: a bare CONTINUE/STOP, or None when unclear."""
    match = _VERDICT.search(answer)
    return match.group(1).lower() if match else None


def build_prompt(goal: str, turn_digest: str) -> tuple[str, str]:
    """(system prompt, user text) for the judge call."""
    return PROMPT, f"Completion condition:\n{goal}\n\nLatest turn:\n{turn_digest}"
