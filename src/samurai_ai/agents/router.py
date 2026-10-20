"""Ordered fallback router for model calls.

The router is NOT a source of truth: it only invokes provider callables in
candidate order and reports every fallback transition through the supplied
event callback. Transient failures (network, HTTP 429/5xx) are retried with
a short deterministic backoff before moving to the next candidate;
capability/auth failures skip the candidate immediately; budget exhaustion
aborts the whole chain without fallback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from samurai_ai.agents.budgets import BudgetExceededError
from samurai_ai.agents.provider import ChatResponse
from samurai_ai.domain.models import (
    CapabilityError,
    ModelError,
    ModelNotFoundError,
    ProviderUnavailableError,
)

MAX_RETRIES = 2
BACKOFF_SECONDS = 0.25


class ModelRouter:
    def __init__(
        self,
        max_retries: int = MAX_RETRIES,
        backoff_seconds: float = BACKOFF_SECONDS,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.sleeper = sleeper

    async def complete(
        self,
        candidates: list[str],
        invoke: Callable[[str], Awaitable[ChatResponse]],
        on_fallback: Callable[[str, str, str], None],
    ) -> ChatResponse:
        if not candidates:
            raise ModelError("model router requires at least one candidate")
        last_error: Exception | None = None
        for index, model_id in enumerate(candidates):
            try:
                return await self._invoke_with_retries(model_id, invoke)
            except BudgetExceededError:
                raise
            except (CapabilityError, ModelNotFoundError) as exc:
                last_error = exc
                reason = "capability"
            except ProviderUnavailableError as exc:
                last_error = exc
                if exc.auth:
                    reason = "auth"
                elif not exc.transient:
                    raise
                else:
                    reason = "unavailable"
            if index + 1 < len(candidates):
                on_fallback(model_id, candidates[index + 1], reason)
        assert last_error is not None
        raise last_error

    async def _invoke_with_retries(
        self,
        model_id: str,
        invoke: Callable[[str], Awaitable[ChatResponse]],
    ) -> ChatResponse:
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                return await invoke(model_id)
            except ProviderUnavailableError as exc:
                if exc.auth or not exc.transient or attempt == attempts - 1:
                    raise
                await self.sleeper(self.backoff_seconds * (attempt + 1))
        raise ModelError("unreachable router state")
