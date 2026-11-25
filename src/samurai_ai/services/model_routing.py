"""Persistent planning-to-execution model routing for an operation."""

from __future__ import annotations

from samurai_ai.domain.models import ModelError, ModelNotFoundError
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.settings import SettingsService


class ModelRoutingService:
    """Keep the one-way model handoff in the operation journal.

    The default model starts a cycle. A recorded handoff selects the execution
    model until an operator reset starts another planning cycle.
    """

    EXECUTION_SETTING = "model.execution"
    HANDOFF_EVENT = "model.handoff"
    RESET_EVENT = "model.route.reset"

    def __init__(
        self,
        settings: SettingsService,
        operations: OperationService,
        models: ModelService,
    ) -> None:
        self.settings = settings
        self.operations = operations
        self.models = models

    def execution_model(self) -> str | None:
        value = self.settings.get_value(self.EXECUTION_SETTING)
        return value.strip() if value and value.strip() else None

    def set_execution_model(self, model_id: str) -> str:
        candidate = model_id.strip()
        if not candidate:
            raise ModelError("execution model is empty")
        self._validate_model(candidate)
        self.settings.set_value(self.EXECUTION_SETTING, candidate)
        return candidate

    def clear_execution_model(self) -> None:
        self.settings.set_value(self.EXECUTION_SETTING, "")

    def active_model(self, operation_id: str) -> str | None:
        """Replay only the routing events; other operation history is irrelevant."""
        configured = self.execution_model()
        if configured is None:
            return self.models.default_model()
        for event in reversed(self.operations.events(operation_id)):
            if event.event_type == self.HANDOFF_EVENT:
                target = str(event.payload.get("to", "")).strip()
                if target == configured:
                    try:
                        self._validate_model(target)
                    except (ModelError, ModelNotFoundError):
                        break
                    return target
            elif event.event_type == self.RESET_EVENT:
                break
        return self.models.default_model()

    def handoff(self, operation_id: str, current_model: str) -> str | None:
        """Record and return the execution route, or decline the transition."""
        target = self.execution_model()
        if not operation_id or not target or target == current_model:
            return None
        if self.active_model(operation_id) != current_model:
            return None
        self._validate_model(target)
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.HANDOFF_EVENT,
                payload={"from": current_model, "to": target, "reason": "shell_observed"},
            )
        )
        return target

    def _validate_model(self, model_id: str) -> None:
        self.models.validate_model_reference(model_id)

    def reset(self, operation_id: str, *, reason: str) -> None:
        """Start a fresh planning cycle without changing the execution setting."""
        if not operation_id:
            return
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.RESET_EVENT,
                payload={"reason": reason},
            )
        )
