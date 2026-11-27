"""Workload shape shared by planning and auxiliary-agent policy.

Document QA produces evidence-backed answers, not vulnerability findings. Keep
this classifier deliberately narrow: reading a flag or a password file is still
part of an attack, not a document-review task.
"""

from __future__ import annotations

import re

_DOCUMENT_OBJECTIVE = re.compile(
    r"(?:\bdocumentation\b|\bdocs?\b|follow(?: the)? instructions\b"
    r"|answer(?: all)?(?: \d+)?(?: [a-z-]+){0,3} questions?\b|summari[sz]e\b"
    r"|документац|инструкц|ответ(?:ь|ьте)?\b.{0,60}\bвопрос)",
    re.IGNORECASE,
)


def is_document_objective(text: str) -> bool:
    """Whether the objective asks for a document answer rather than an attack."""
    return bool(_DOCUMENT_OBJECTIVE.search(" ".join(text.split())))
