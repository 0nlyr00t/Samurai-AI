from __future__ import annotations

from collections.abc import Iterable

from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.models import (
    CapabilityError,
    ModelCapability,
    ModelDefinition,
    ModelNotFoundError,
)


def require_capabilities(
    model: ModelDefinition, required: Iterable[ModelCapability]
) -> None:
    missing = tuple(
        sorted(
            capability.value
            for capability in required
            if capability not in model.capabilities
        )
    )
    if missing:
        raise CapabilityError(model.id, missing)


def select_model(
    registry: ModelRegistry,
    required: Iterable[ModelCapability] = (),
    *,
    prefer_local: bool = False,
) -> ModelDefinition:
    needed = frozenset(required)
    candidates = [
        model
        for model in registry.list_models()
        if needed <= model.capabilities
    ]
    if prefer_local:
        candidates.sort(key=lambda model: (not model.local, _cost(model)))
    else:
        candidates.sort(key=_cost)
    if not candidates:
        raise ModelNotFoundError(
            f"no registered model supports capabilities: "
            f"{', '.join(sorted(c.value for c in needed)) or '(none)'}"
        )
    return candidates[0]


def _cost(model: ModelDefinition) -> float:
    return model.cost_input_per_m + model.cost_output_per_m
