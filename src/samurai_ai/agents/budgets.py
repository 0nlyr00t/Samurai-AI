from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from samurai_ai.domain.models import ModelError
from samurai_ai.domain.operations import OperationEvent

BUDGET_KEY_PREFIX = "budget."


class BudgetExceededError(ModelError):
    pass


class ModelBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_tokens_total: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    max_calls: int | None = Field(default=None, ge=1)

    def is_unset(self) -> bool:
        return (
            self.max_tokens_total is None
            and self.max_cost_usd is None
            and self.max_calls is None
        )


class UsageSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def usage_from_events(events: Iterable[OperationEvent]) -> UsageSummary:
    calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    cost = 0.0
    by_model: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event_type != "model.call":
            continue
        payload = event.payload
        calls += 1
        prompt = int(payload.get("prompt_tokens") or 0)
        completion = int(payload.get("completion_tokens") or 0)
        estimated = float(payload.get("estimated_cost_usd") or 0.0)
        prompt_tokens += prompt
        completion_tokens += completion
        cost += estimated
        model_id = str(payload.get("model_id") or "unknown")
        entry = by_model.setdefault(
            model_id, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
        )
        entry["calls"] += 1
        entry["prompt_tokens"] += prompt
        entry["completion_tokens"] += completion
        entry["cost_usd"] = round(entry["cost_usd"] + estimated, 6)
    return UsageSummary(
        calls=calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=round(cost, 6),
        by_model=by_model,
    )


def check_budget(budget: ModelBudget, usage: UsageSummary) -> None:
    if budget.max_calls is not None and usage.calls >= budget.max_calls:
        raise BudgetExceededError(
            f"model call budget exhausted: {usage.calls}/{budget.max_calls} calls used"
        )
    if budget.max_tokens_total is not None and usage.total_tokens >= budget.max_tokens_total:
        raise BudgetExceededError(
            f"model token budget exhausted: {usage.total_tokens}/"
            f"{budget.max_tokens_total} tokens used"
        )
    if budget.max_cost_usd is not None and usage.cost_usd >= budget.max_cost_usd:
        raise BudgetExceededError(
            f"model cost budget exhausted: ${usage.cost_usd}/${budget.max_cost_usd} used"
        )
