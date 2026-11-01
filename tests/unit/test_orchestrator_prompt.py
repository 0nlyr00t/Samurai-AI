"""The executor prompt names the parameters a tool actually accepts."""

from __future__ import annotations

from samurai_ai.agents.orchestrator import _EXECUTOR_INSTRUCTION, _parameter_hint


def test_required_parameters_are_marked_first() -> None:
    hint = _parameter_hint(
        {"type": "object", "required": ["url"], "properties": {"url": {}, "method": {}}}
    )

    assert hint == " (params: url*, method; * required)"


def test_optional_parameters_are_listed_without_a_marker() -> None:
    assert _parameter_hint({"properties": {"text": {}}}) == " (params: text; * required)"


def test_types_and_allowed_values_travel_with_the_name() -> None:
    hint = _parameter_hint(
        {
            "required": ["targets"],
            "properties": {
                "targets": {"type": "array", "items": {"type": "string"}},
                "profile": {"type": "string", "enum": ["headers", "tech"]},
            },
        }
    )

    assert hint == (
        " (params: targets*:array<string>, profile:string one of headers|tech; * required)"
    )


def test_a_tool_without_parameters_says_so() -> None:
    assert _parameter_hint({}) == " (no parameters)"


def test_the_instruction_forbids_placeholders() -> None:
    assert "literal" in _EXECUTOR_INSTRUCTION
    assert "https://example.com/" in _EXECUTOR_INSTRUCTION
    assert "tool_id" in _EXECUTOR_INSTRUCTION
