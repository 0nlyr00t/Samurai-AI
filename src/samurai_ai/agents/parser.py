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
_COMMAND_TYPES = frozenset(
    {
        "request_tool_run",
        "note",
        "propose_finding",
        "record_impact",
        "complete_task",
        "fail",
    }
)


def _extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").strip()
        if candidate.startswith("json"):
            candidate = candidate[4:].strip()
    try:
        # models leave raw newlines and tabs inside strings; strict=False is the
        # only difference, structure is still validated afterwards
        decoded = json.loads(candidate, strict=False)
    except json.JSONDecodeError as exc:
        raise AgentOutputError(f"model output is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise AgentOutputError("model output must be a single JSON object")
    return decoded


def parse_planner_output(text: str) -> PlannerOutput:
    payload = _stringify_steps(_extract_json_object(text))
    try:
        return PlannerOutput.model_validate(payload)
    except ValidationError as exc:
        raise AgentOutputError(f"planner output failed schema validation: {exc}") from exc


def _lift_bare_commands(payload: dict[str, Any]) -> dict[str, Any]:
    """A command written as a bare type string becomes ``{"type": ...}``.

    Small models do this constantly, and the command schema itself stays strict
    — only the wrapping shape is repaired, never the fields.
    """
    commands = payload.get("commands")
    if isinstance(commands, list):
        payload = dict(payload)
        payload["commands"] = [
            _repair_command(item) for item in commands
        ]
    return payload


def _stringify_steps(payload: dict[str, Any]) -> dict[str, Any]:
    """A step written as an object becomes the sentence it was meant to be."""
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return payload
    repaired = []
    for step in steps:
        if isinstance(step, dict):
            repaired.append(", ".join(f"{key}={value}" for key, value in step.items()))
        else:
            repaired.append(step)
    return {**payload, "steps": repaired}


def _unwrap_json_tool_id(item: dict[str, Any]) -> dict[str, Any]:
    """A whole command object pasted into ``tool_id`` replaces the command."""
    tool_id = item.get("tool_id")
    if not (isinstance(tool_id, str) and tool_id.lstrip().startswith("{")):
        return item
    try:
        inner = json.loads(tool_id, strict=False)
    except json.JSONDecodeError:
        return item
    if isinstance(inner, dict) and isinstance(inner.get("type"), str):
        return inner
    return item


def _repair_command(item: Any) -> Any:
    """Repair the two shapes small models reach for instead of a command."""
    if isinstance(item, str):
        if item in _COMMAND_TYPES:
            return {"type": item}
        return {"type": "request_tool_run", "tool_id": item}
    if not isinstance(item, dict):
        return item
    item = _unwrap_json_tool_id(item)
    known = item.get("type")
    if isinstance(known, str) and known not in _COMMAND_TYPES and not item.get("tool_id"):
        # `{"type": "local.echo", "params": {...}}` — a tool id where the
        # command name belongs; the fields are already in the right place
        return {**item, "type": "request_tool_run", "tool_id": known}
    return item


def parse_executor_output(text: str) -> tuple[AgentCommand, ...]:
    payload = _lift_bare_commands(_extract_json_object(text))
    # an envelope key the model added (a blackboard echo, a note) is dropped;
    # the command objects themselves stay strict
    payload = {key: value for key, value in payload.items() if key == "commands"}
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
