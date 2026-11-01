"""Bounded evidence that a turn is going in circles, and why a turn ended.

The model has no clock and no memory of what it already tried: a third
`curl` against the same host looks exactly like the first. Nothing here
decides anything — the detector only names the repetition, the loop hands
it to the model and the operator, and the turn carries on.

The shape follows ARTEX's `detectStagnation` (agent/tools.go): count
repeats of one direction, and disarm the whole thing as soon as a finding
appears, because a direction that produced something is not stuck.
"""

from __future__ import annotations

import shlex
from collections import Counter, deque
from enum import StrEnum

#: How many identical directions in the window count as a loop.
THRESHOLD = 3
#: The window a repeat is counted in; small so an old streak cannot fire.
WINDOW = 8


class TerminalReason(StrEnum):
    """Why a turn stopped. Lives in the transcript as `chat.turn.end`."""

    COMPLETED = "completed"
    ROUNDS = "rounds"
    MODEL_ERROR = "model-error"
    DENIED = "denied"
    CANCELLED = "cancelled"


def direction(command: str) -> str | None:
    """The shape of a command: binary plus its first non-flag argument.

    Flags are dropped, so `nmap -sV 10.0.0.1` and `nmap -p- 10.0.0.1` are the
    same direction while `curl` against two hosts is not.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    binary = tokens[0].rsplit("/", 1)[-1].lower()
    plain = [token for token in tokens[1:] if not token.startswith("-")]
    if not plain:
        return binary
    # A flag value (`-p 443`) sits among the plain tokens too; a target usually
    # carries a dot, a slash or a colon, so prefer the first such token and only
    # fall back to the last plain one.
    target = next((token for token in plain if any(mark in token for mark in "./:")), plain[-1])
    return f"{binary} {target.lower()}"


class Stagnation:
    """Counts repeats of the current direction and reports each streak once."""

    def __init__(self, threshold: int = THRESHOLD, window: int = WINDOW) -> None:
        self._window: deque[str] = deque(maxlen=window)
        self._reported: set[str] = set()
        self._threshold = threshold
        #: The direction that went stale since the loop last looked.
        self.last_stuck: str | None = None

    def record(self, command: str) -> str | None:
        """Feed one executed command; the direction when it just went stale."""
        shape = direction(command)
        if shape is None:
            return None
        self._window.append(shape)
        if shape in self._reported:
            return None
        if Counter(self._window)[shape] < self._threshold:
            return None
        self._reported.add(shape)
        return shape

    def finding_seen(self) -> None:
        """A finding disarms every streak: the direction did produce something."""
        self._reported.clear()

    def reset(self) -> None:
        self._window.clear()
        self._reported.clear()
