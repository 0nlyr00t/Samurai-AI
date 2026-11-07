"""Replay an operation from its event journal.

The journal is append-only and every step of a run is an event, so a replay is
a scrub through that history: at each tick the operator sees what the run had
done so far. Read-only; nothing re-executes.
"""

from __future__ import annotations

from samurai_ai.domain.operations import OperationEvent


def replay_lines(events: list[OperationEvent], upto: int | None = None) -> list[str]:
    """One line per event, in order; ``upto`` scrubs to a prefix."""
    chosen = events if upto is None else events[: max(0, upto)]
    lines: list[str] = []
    for event in chosen:
        stamp = event.created_at.strftime("%H:%M:%S")
        payload = " ".join(f"{k}={v}" for k, v in event.payload.items()) if event.payload else ""
        lines.append(f"{stamp}  {event.event_type}" + (f"  {payload[:120]}" if payload else ""))
    return lines
