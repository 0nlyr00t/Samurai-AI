"""Phase overrides: when the operator knows the rail is wrong.

The rail itself is derived from proven state, so an override is the exception —
it is journaled with actor and reason, and it is not a second source of phase
truth: clearing it puts the derived phase back in charge.
"""

from __future__ import annotations

from samurai_ai.domain.kernel import KernelError
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.tui.killchain import chains_for


class PhaseOverrideError(KernelError):
    """The phase is not one of the mode's phases."""


class KillchainService:
    """Operator phase overrides on top of the evidence-derived rail."""

    def __init__(self, operations: OperationService, kernel: KernelService) -> None:
        self.operations = operations
        self.kernel = kernel

    def phases(self, operation_id: str) -> tuple[str, ...]:
        operation = self.operations.get(operation_id)
        return chains_for(operation.mode)

    def override(self, operation_id: str, phase: str, reason: str, actor: str = "operator") -> None:
        """Put the rail on a phase the operator names, with the reason recorded."""
        phases = self.phases(operation_id)
        if phase not in phases:
            raise PhaseOverrideError(
                f"phase {phase!r} is not in this mode's chain: {', '.join(phases)}"
            )
        if not reason.strip():
            raise PhaseOverrideError("an override needs a reason")
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type="killchain.phase.override",
                payload={"phase": phase, "reason": reason.strip()[:200], "actor": actor},
            )
        )

    def clear(self, operation_id: str, actor: str = "operator") -> None:
        """Drop the override and hand the phase back to the evidence."""
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type="killchain.phase.auto",
                payload={"actor": actor},
            )
        )

    def active(self, operation_id: str) -> tuple[str, str] | None:
        """The override still in force, as (phase, reason); None when derived."""
        latest: tuple[str, str] | None = None
        for event in self.operations.repository.events(operation_id):
            if event.event_type == "killchain.phase.override":
                latest = (str(event.payload.get("phase", "")), str(event.payload.get("reason", "")))
            elif event.event_type == "killchain.phase.auto":
                latest = None
        return latest
