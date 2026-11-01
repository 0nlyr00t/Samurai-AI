import pytest
from pydantic import ValidationError

from samurai_ai.agents.blackboard import (
    Blackboard,
    make_note_event,
    validate_note_key,
)
from samurai_ai.agents.parser import (
    corrective_prompt,
    parse_executor_output,
    parse_planner_output,
)
from samurai_ai.domain.agents import (
    AgentError,
    AgentOutputError,
    AgentRun,
    AgentRunStatus,
    ExecutorOutput,
    PlannerOutput,
)
from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.operations import OperationEvent


def test_command_schema_accepts_whitelist() -> None:
    output = ExecutorOutput.model_validate(
        {
            "commands": [
                {"type": "request_tool_run", "tool_id": "local.echo", "params": {"text": "x"}},
                {"type": "note", "key": "shared.k", "value": "v"},
                {
                    "type": "propose_finding",
                    "title": "T",
                    "summary": "s",
                    "severity": "high",
                    "artifact_ids": ["art_1"],
                },
                {"type": "complete_task"},
                {"type": "fail", "reason": "stuck"},
            ]
        }
    )
    assert len(output.commands) == 5
    assert output.commands[3].type == "complete_task"


def test_command_schema_rejects_unknown_and_malformed() -> None:
    with pytest.raises(ValidationError):
        ExecutorOutput.model_validate({"commands": [{"type": "delete_everything"}]})
    with pytest.raises(ValidationError):
        ExecutorOutput.model_validate({"commands": []})
    with pytest.raises(ValidationError):
        ExecutorOutput.model_validate(
            {"commands": [{"type": "note", "key": "k", "value": "v", "evil": 1}]}
        )
    with pytest.raises(ValidationError):
        ExecutorOutput.model_validate(
            {"commands": [{"type": "propose_finding", "title": "t", "severity": "apocalyptic"}]}
        )
    with pytest.raises(ValidationError):
        ExecutorOutput.model_validate({"commands": [{"type": "request_tool_run"}]})


def test_planner_output_schema() -> None:
    output = PlannerOutput.model_validate({"steps": ["a", "b"]})
    assert output.steps == ("a", "b")
    with pytest.raises(ValidationError):
        PlannerOutput.model_validate({"steps": []})
    with pytest.raises(ValidationError):
        PlannerOutput.model_validate({"steps": ["a"], "extra": 1})


def test_parser_rejects_bad_json_and_schema() -> None:
    with pytest.raises(AgentOutputError, match="not valid JSON"):
        parse_executor_output("sure! here are your commands")
    with pytest.raises(AgentOutputError, match="single JSON object"):
        parse_executor_output("[1, 2]")
    # an unknown type is treated as a tool id, so the schema failure needs a
    # command whose fields are actually wrong
    with pytest.raises(AgentOutputError, match="schema validation"):
        parse_executor_output('{"commands": [{"type": "note"}]}')
    with pytest.raises(AgentOutputError, match="schema validation"):
        parse_planner_output('{"commands": []}')


def test_parser_accepts_fenced_json() -> None:
    commands = parse_executor_output(
        '```json\n{"commands": [{"type": "complete_task"}]}\n```'
    )
    assert commands[0].type == "complete_task"
    steps = parse_planner_output('{"steps": ["recon"]}')
    assert steps.steps == ("recon",)


def test_parser_lifts_a_bare_command_string() -> None:
    commands = parse_executor_output(
        '{"commands": ["complete_task", {"type": "note", "key": "k", "value": "v"}]}'
    )

    assert [command.type for command in commands] == ["complete_task", "note"]


def test_parser_drops_an_extra_envelope_key() -> None:
    commands = parse_executor_output(
        '{"commands": ["complete_task"], "blackboard": {"surface": "x"}}'
    )

    assert [command.type for command in commands] == ["complete_task"]


def test_parser_turns_a_bare_tool_id_into_a_tool_run() -> None:
    commands = parse_executor_output('{"commands": ["nmap_scan"]}')

    assert commands[0].type == "request_tool_run"
    assert commands[0].tool_id == "nmap_scan"  # type: ignore[union-attr]


def test_parser_turns_a_tool_id_in_type_into_a_tool_run() -> None:
    commands = parse_executor_output(
        '{"commands": [{"type": "local.echo", "params": {"text": "hi"}}]}'
    )

    assert commands[0].type == "request_tool_run"
    assert commands[0].tool_id == "local.echo"  # type: ignore[union-attr]


def test_corrective_prompt_carries_error() -> None:
    try:
        parse_executor_output("garbage")
    except AgentOutputError as exc:
        prompt = corrective_prompt("original", exc)
    assert "original" in prompt
    assert "not valid JSON" in prompt


def test_agent_run_finish_transitions() -> None:
    run = AgentRun(operation_id="op_1", task_id="t1")
    assert run.status is AgentRunStatus.RUNNING
    run.finish(AgentRunStatus.COMPLETED)
    assert run.finished_at is not None
    with pytest.raises(AgentError, match="already finished"):
        run.finish(AgentRunStatus.FAILED)


def test_blackboard_replay_and_namespace_isolation() -> None:
    events = [
        OperationEvent(
            operation_id="op_1",
            event_type="agent.note",
            payload={"run_id": "a", "key": "shared.plan", "value": "v1"},
        ),
        OperationEvent(
            operation_id="op_1",
            event_type="agent.note",
            payload={"run_id": "a", "key": "run.a.local", "value": "va"},
        ),
        OperationEvent(
            operation_id="op_1",
            event_type="agent.note",
            payload={"run_id": "b", "key": "run.b.secret", "value": "vb"},
        ),
        OperationEvent(operation_id="op_1", event_type="tool.run.completed", payload={}),
    ]
    blackboard = Blackboard.from_events(events)
    visible_a = blackboard.visible_for("a")
    assert visible_a == {"shared.plan": "v1", "run.a.local": "va"}
    assert blackboard.keys_for("b") == ["run.b.secret", "shared.plan"]
    assert blackboard.get("run.b.secret", "a") is None


def test_note_event_redacts_secrets() -> None:
    redactor = SecretRedactor()
    redactor.register("hunter2")
    event = make_note_event("op_1", "run_1", "shared.creds", "password is hunter2", redactor)
    assert event.payload["value"] == "password is ***REDACTED***"
    assert event.payload["redaction_count"] == 1


def test_validate_note_key() -> None:
    assert validate_note_key("shared.x") == "shared.x"
    assert validate_note_key("run.abc.deep.key") == "run.abc.deep.key"
    for bad in ("", "a..b", "run.", "run.abc"):
        with pytest.raises(ValueError):
            validate_note_key(bad)
