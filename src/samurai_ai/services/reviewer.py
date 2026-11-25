"""Per-operation reviewer configuration and redacted review events."""

from __future__ import annotations

from uuid import uuid4

from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.reviewer import OpenBlocker, ReviewerConfig, ReviewNote, ReviewSeverity
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService


class ReviewerService:
    ENABLED = "reviewer.enabled"
    DISABLED = "reviewer.disabled"
    NOTE = "reviewer.note"
    ERROR = "reviewer.error"
    RESOLVED = "reviewer.resolved"

    def __init__(self, operations: OperationService, models: ModelService) -> None:
        self.operations = operations
        self.models = models

    def config(self, operation_id: str) -> ReviewerConfig | None:
        for event in reversed(self.operations.events(operation_id)):
            if event.event_type == self.ENABLED:
                model = str(event.payload.get("model", "")).strip()
                fallback = str(event.payload.get("fallback", "")).strip() or None
                if model:
                    return ReviewerConfig(model, fallback)
            elif event.event_type == self.DISABLED:
                return None
        return None

    def enable(self, operation_id: str, config: ReviewerConfig) -> ReviewerConfig:
        self.operations.get(operation_id)
        self.models.validate_model_reference(config.model)
        if config.fallback:
            self.models.validate_model_reference(config.fallback)
        normalized = ReviewerConfig(
            config.model,
            config.fallback if config.fallback != config.model else None,
        )
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.ENABLED,
                payload={"model": normalized.model, "fallback": normalized.fallback},
            )
        )
        return normalized

    def disable(self, operation_id: str) -> bool:
        self.operations.get(operation_id)
        if self.config(operation_id) is None:
            return False
        self.operations.repository.append_event(
            OperationEvent(operation_id=operation_id, event_type=self.DISABLED)
        )
        return True

    def record_note(self, operation_id: str, note: ReviewNote, text: str) -> str:
        """Journal a note; returns its id (a blocker is answered by that id)."""
        note_id = f"rv_{uuid4().hex[:8]}"
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.NOTE,
                payload={"id": note_id, "severity": note.severity.value, "text": text},
            )
        )
        return note_id

    def open_blockers(self, operation_id: str) -> list[OpenBlocker]:
        """Blockers nobody has answered yet, oldest first."""
        raised: dict[str, OpenBlocker] = {}
        for event in self.operations.events(operation_id):
            note_id = str(event.payload.get("id") or "")
            if event.event_type == self.NOTE and note_id:
                if event.payload.get("severity") == ReviewSeverity.BLOCKER.value:
                    raised[note_id] = OpenBlocker(note_id, str(event.payload.get("text", "")))
            elif event.event_type == self.RESOLVED:
                raised.pop(str(event.payload.get("note_id") or ""), None)
        return list(raised.values())

    def resolve(self, operation_id: str, note_id: str, resolution: str, actor: str) -> None:
        """Close an open blocker with the reason it no longer holds.

        ``actor`` says who closed it: the agent answering it, or the operator
        dismissing it. Both are journaled; only an open blocker can close.
        """
        resolution = resolution.strip()
        if not resolution:
            raise ValueError("a resolution must say how the blocker was addressed")
        if note_id not in {blocker.id for blocker in self.open_blockers(operation_id)}:
            raise ValueError(f"no open reviewer blocker {note_id!r}")
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.RESOLVED,
                payload={"note_id": note_id, "resolution": resolution[:1000], "by": actor},
            )
        )

    def record_error(self, operation_id: str, reason: str) -> None:
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.ERROR,
                payload={"reason": reason[:200]},
            )
        )
