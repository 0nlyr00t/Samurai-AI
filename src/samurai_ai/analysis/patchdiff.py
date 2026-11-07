"""Turn a patch diff into a vulnerability hypothesis for 1-day work.

Pure text analysis, no network: which files and functions the patch touches,
what kind of guard was added (bounds, null, length, auth), and what input the
unpatched code plausibly mishandles. The agent reads this to aim the exploit,
not to copy an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A guard the patch adds names the bug class the old code had.
_GUARDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bounds", ("len", "size", "bounds", "overflow", "limit", "capacity")),
    ("null", ("null", "none", "nullptr")),
    ("length", ("strlen", "length", "count")),
    ("auth", ("auth", "permission", "allowed", "check")),
    ("type", ("type", "isinstance", "cast", "convert")),
)

_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)
_DIFF_FUNC = re.compile(r"^@@.*@@\s*(?:.*\()?\s*([A-Za-z_][A-Za-z0-9_]*)?", re.M)
_ADDED = re.compile(r"^\+([^+\n].*)?$", re.M)
_REMOVED = re.compile(r"^-([^-\n].*)?$", re.M)


@dataclass
class PatchAnalysis:
    files: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)
    hypothesis: str = ""


def analyze_patch(diff_text: str, advisory: str = "") -> PatchAnalysis:
    """Read a unified diff plus optional advisory into a hypothesis."""
    analysis = PatchAnalysis()
    analysis.files = sorted({m.group(2) for m in _DIFF_FILE.finditer(diff_text)})
    analysis.functions = sorted(
        {m.group(1) for m in _DIFF_FUNC.finditer(diff_text) if m.group(1)}
    )
    analysis.added = [m.group(1).strip() for m in _ADDED.finditer(diff_text)][:40]
    analysis.removed = [m.group(1).strip() for m in _REMOVED.finditer(diff_text)][:40]

    added_text = "\n".join(analysis.added).lower()
    analysis.guards = [
        name for name, markers in _GUARDS if any(marker in added_text for marker in markers)
    ]

    hint = advisory.lower()
    pieces: list[str] = []
    if analysis.guards:
        pieces.append("patch adds " + ", ".join(analysis.guards) + " guards")
    if "rce" in hint or "remote code execution" in hint:
        pieces.append("advisory says RCE")
    if "overflow" in hint:
        pieces.append("advisory says overflow")
    if analysis.files:
        pieces.append("touches " + ", ".join(analysis.files[:4]))
    analysis.hypothesis = (
        "; ".join(pieces) if pieces else "a defensive change without a named class"
    )
    return analysis


def report(analysis: PatchAnalysis) -> str:
    """A compact, human-readable readout for the journal."""
    lines = ["patch analysis:"]
    if analysis.files:
        lines.append("  files: " + ", ".join(analysis.files))
    if analysis.functions:
        lines.append("  functions: " + ", ".join(analysis.functions))
    if analysis.guards:
        lines.append("  guards added: " + ", ".join(analysis.guards))
    if analysis.added:
        lines.append("  added (top):")
        for line in analysis.added[:8]:
            lines.append("    + " + line)
    if analysis.removed:
        lines.append("  removed (top):")
        for line in analysis.removed[:8]:
            lines.append("    - " + line)
    lines.append("  hypothesis: " + analysis.hypothesis)
    return "\n".join(lines)
