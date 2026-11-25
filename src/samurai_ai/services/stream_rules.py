"""Per-operation stream-rule configuration stored in the event journal."""

from __future__ import annotations

from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.stream_rules import MAX_RULES, StreamRule, StreamRuleError
from samurai_ai.services.operations import OperationService


class StreamRuleService:
    ADDED = "guard.rule.added"
    REMOVED = "guard.rule.removed"
    TRIGGERED = "guard.triggered"

    def __init__(self, operations: OperationService) -> None:
        self.operations = operations

    def rules(self, operation_id: str) -> tuple[StreamRule, ...]:
        rules: dict[str, StreamRule] = {}
        for event in self.operations.events(operation_id):
            if event.event_type == self.ADDED:
                try:
                    rule = StreamRule.from_payload(event.payload)
                except StreamRuleError:
                    continue
                rules[rule.name] = rule
            elif event.event_type == self.REMOVED:
                rules.pop(str(event.payload.get("name", "")), None)
        return tuple(rules.values())

    def add(self, operation_id: str, rule: StreamRule) -> StreamRule:
        self.operations.get(operation_id)
        existing = {item.name for item in self.rules(operation_id)}
        if rule.name not in existing and len(existing) >= MAX_RULES:
            raise StreamRuleError(f"at most {MAX_RULES} stream rules per operation")
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.ADDED,
                payload=rule.payload(),
            )
        )
        return rule

    def remove(self, operation_id: str, name: str) -> bool:
        self.operations.get(operation_id)
        if name not in {item.name for item in self.rules(operation_id)}:
            return False
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=self.REMOVED,
                payload={"name": name},
            )
        )
        return True
