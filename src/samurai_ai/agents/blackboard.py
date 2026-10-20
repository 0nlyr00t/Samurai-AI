"""Shared blackboard for agent runs.

The operation event journal is the source of truth: notes are appended as
``agent.note`` events (redacted first) and the blackboard view is rebuilt by
replaying them. Visibility is namespaced: a run sees ``shared.*`` plus its
own ``run.<run_id>.*`` namespace and never other runs' namespaces.
"""

from __future__ import annotations

from collections.abc import Iterable

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.operations import OperationEvent

SHARED_PREFIX = "shared."
RUN_PREFIX = "run."


def validate_note_key(key: str) -> str:
    if not key or key.endswith(".") or ".." in key:
        raise ValueError(f"invalid blackboard key: {key!r}")
    if key.startswith(RUN_PREFIX):
        remainder = key[len(RUN_PREFIX):]
        owner, dot, rest = remainder.partition(".")
        if not owner or not dot or not rest:
            raise ValueError(f"invalid run-scoped blackboard key: {key!r}")
    return key


class Blackboard:
    def __init__(self, notes: dict[str, str] | None = None) -> None:
        self._notes = notes or {}

    @classmethod
    def from_events(cls, events: Iterable[OperationEvent]) -> Blackboard:
        notes: dict[str, str] = {}
        for event in events:
            if event.event_type != "agent.note":
                continue
            key = event.payload.get("key")
            value = event.payload.get("value")
            if isinstance(key, str) and isinstance(value, str):
                notes[key] = value
        return cls(notes)

    def visible_for(self, run_id: str) -> dict[str, str]:
        prefix = f"{RUN_PREFIX}{run_id}."
        return {
            key: value
            for key, value in sorted(self._notes.items())
            if key.startswith(SHARED_PREFIX) or key.startswith(prefix)
        }

    def get(self, key: str, run_id: str) -> str | None:
        return self.visible_for(run_id).get(key)

    def keys_for(self, run_id: str) -> list[str]:
        return sorted(self.visible_for(run_id))


def make_note_event(
    operation_id: str,
    run_id: str,
    key: str,
    value: str,
    redactor: SecretRedactor,
) -> OperationEvent:
    validate_note_key(key)
    result = redactor.redact(value)
    return OperationEvent(
        operation_id=operation_id,
        event_type="agent.note",
        payload={
            "run_id": run_id,
            "key": key,
            "value": result.text,
            "redaction_count": result.count,
        },
    )
