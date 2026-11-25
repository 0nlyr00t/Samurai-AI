"""Per-operation reviewer configuration and redacted review events."""

from __future__ import annotations

from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.reviewer import ReviewerConfig, ReviewNote
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService


class ReviewerService:
    ENABLED = "reviewer.enabled"
    DISABLED = "reviewer.disabled"
    NOTE = "reviewer.note"
    ERROR = "reviewer.error"

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

    def record_note(self, operation_id: str, note: ReviewNote, text: str) -> None:
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.NOTE,
                payload={"severity": note.severity.value, "text": text},
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
