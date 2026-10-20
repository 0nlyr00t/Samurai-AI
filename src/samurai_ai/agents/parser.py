"""Parser role: strict structured-output parsing with bounded retries.

Model text must be a single JSON object matching the PlannerOutput or
ExecutorOutput schema (no prose, no markdown fences). Schema mismatches and
invalid JSON raise AgentOutputError; the orchestrator retries with a
corrective prompt at most twice and then fails the step — never silently.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from samurai_ai.domain.agents import (
    AgentCommand,
    AgentOutputError,
    ExecutorOutput,
    PlannerOutput,
)

MAX_PARSE_RETRIES = 2


def _extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").strip()
        if candidate.startswith("json"):
            candidate = candidate[4:].strip()
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AgentOutputError(f"model output is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise AgentOutputError("model output must be a single JSON object")
    return decoded


def parse_planner_output(text: str) -> PlannerOutput:
    payload = _extract_json_object(text)
    try:
        return PlannerOutput.model_validate(payload)
    except ValidationError as exc:
        raise AgentOutputError(f"planner output failed schema validation: {exc}") from exc


def parse_executor_output(text: str) -> tuple[AgentCommand, ...]:
    payload = _extract_json_object(text)
    try:
        output = ExecutorOutput.model_validate(payload)
    except ValidationError as exc:
        raise AgentOutputError(f"executor output failed schema validation: {exc}") from exc
    return output.commands


def corrective_prompt(original: str, error: AgentOutputError) -> str:
    return (
        "Your previous output was rejected by the output parser.\n"
        f"Parser error: {error}\n"
        "Respond with exactly one JSON object matching the required schema, "
        "no prose and no markdown fences.\n"
        f"Original request:\n{original}"
    )
