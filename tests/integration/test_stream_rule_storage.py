from pathlib import Path

from test_model_routing import _stack

from samurai_ai.domain.stream_rules import RuleMode, RuleScope, StreamRule
from samurai_ai.services.stream_rules import StreamRuleService


def test_rules_replay_add_replace_and_remove(tmp_path: Path) -> None:
    operations, _models, _settings, operation_id = _stack(tmp_path / "rules.db")
    service = StreamRuleService(operations)
    first = StreamRule(
        name="scope-note",
        pattern="example",
        reminder="Check the ROE",
        scope=RuleScope.ALL,
        mode=RuleMode.REMIND,
    )
    service.add(operation_id, first)
    assert service.rules(operation_id) == (first,)

    replacement = StreamRule(
        name=first.name,
        pattern="other",
        reminder="Stop before this call",
        scope=RuleScope.TOOLS,
        mode=RuleMode.INTERRUPT,
    )
    service.add(operation_id, replacement)
    assert service.rules(operation_id) == (replacement,)
    assert service.remove(operation_id, first.name)
    assert service.rules(operation_id) == ()
    assert not service.remove(operation_id, first.name)

    events = [
        event
        for event in operations.events(operation_id)
        if event.event_type.startswith("guard.rule.")
    ]
    assert [event.event_type for event in events] == [
        "guard.rule.added",
        "guard.rule.added",
        "guard.rule.removed",
    ]
