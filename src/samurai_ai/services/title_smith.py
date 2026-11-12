"""Name an operation's tab from the task, not from its first words.

`" ".join(text.split()[:6])` makes a poor tab name: "получить user.txt и
root.txt" says little and cuts mid-thought. A cheap model reads the task and
answers a short gist (the target and the goal); when it is unavailable the
caller keeps the first-words placeholder, so a broken smith never blocks the
tab or the turn.
"""

from __future__ import annotations

#: a tab is a few words; longer answers are clamped with an ellipsis
MAX_TITLE_CHARS = 48

PROMPT = (
    "You name a security operation's tab from the operator's task. Reply with a "
    "short title of three to six words that captures the essence — the target and "
    "the goal — not the first words of the task. Keep the task's own language. No "
    "quotes, no trailing punctuation, no prose: the title alone."
)


def build_prompt(task: str) -> tuple[str, str]:
    """(system prompt, user text) for the title call."""
    return PROMPT, f"Task:\n{task[:1000]}\n\nTitle:"


def clean(answer: str, *, limit: int = MAX_TITLE_CHARS) -> str:
    """One tidy line from the model's answer, clamped; empty when unusable."""
    stripped = answer.strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0].strip()
    # the model sometimes echoes a "Title:" label or wraps the answer in quotes
    if ":" in line[:8]:
        line = line.split(":", 1)[1].strip()
    line = line.strip("\"'`").strip()
    line = line.rstrip(".,;:!").strip()
    if len(line) > limit:
        line = line[:limit].rstrip() + "…"
    return line
