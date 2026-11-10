"""Exploration graph service: the invariants the model cannot talk its way past.

Every rule in `md/exploration-graph.md` is a check here, not a prompt line: a
fact needs an intent that was actually running, a `done` intent needs a fact, a
finding needs a fact that carries an artifact, a task cannot close over open
intents. State moves are compare-and-set, so an operator pressing stop while the
model works comes back to the model as text instead of being overwritten.
"""

from __future__ import annotations

from datetime import UTC, datetime

from samurai_ai.domain.graph import (
    FACT_SUMMARY_MAX,
    FRONTIER_MAX_CHARS,
    HINT_TEXT_MAX,
    INTENT_TITLE_MAX,
    MAX_ARTIFACT_REFS,
    MAX_INTENTS_PER_TASK,
    MAX_OPEN_INTENTS,
    EdgeRel,
    GraphConflictError,
    GraphInvariantError,
    GraphLimitError,
    GraphNode,
    GraphNotFoundError,
    NodeKind,
    NodeState,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.graph import GraphRepository


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GraphService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        graph: GraphRepository,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.graph = graph

    # --- reading ----------------------------------------------------------

    def get(self, node_id: str) -> GraphNode:
        node = self.graph.get(node_id)
        if node is None:
            raise GraphNotFoundError(f"graph node not found: {node_id}")
        return node

    def for_task(self, task_id: str) -> list[GraphNode]:
        return self.graph.for_task(task_id)

    def for_operation(self, operation_id: str) -> list[GraphNode]:
        return self.graph.for_operation(operation_id)

    def blocking_intents(self, task_id: str) -> list[GraphNode]:
        """Intents that keep a task open — named in the refusal, not counted."""
        return [node for node in self.graph.for_task(task_id) if node.open]

    def facts_of(self, intent_id: str) -> list[GraphNode]:
        """The facts an intent yielded — its evidence, confirmed or not."""
        return self.graph.targets(intent_id, EdgeRel.YIELDS.value)

    def lineage(self, node_id: str) -> list[GraphNode]:
        """The chain from the node up to its origin, nearest ancestor first."""
        path: list[GraphNode] = []
        seen: set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            for parent in (
                *self.graph.sources(current, EdgeRel.SPAWNS.value),
                *self.graph.sources(current, EdgeRel.YIELDS.value),
                *self.graph.sources(current, EdgeRel.DERIVED_FROM.value),
            ):
                if parent.id in seen:
                    continue
                path.append(parent)
                frontier.append(parent.id)
        return path

    def frontier(self, task_id: str) -> str:
        """The bounded prompt block: hints, open directions, newest results."""
        nodes = self.graph.for_task(task_id)
        # Active hints steer; consumed ones stay a little longer, otherwise the
        # model loses the instruction one turn after it acted on it.
        active = [n for n in nodes if n.kind is NodeKind.HINT and n.state is NodeState.ACTIVE]
        spent = [n for n in nodes if n.kind is NodeKind.HINT and n.state is NodeState.CONSUMED][
            -2:
        ]
        hints = active + spent
        open_intents = [n for n in nodes if n.open]
        facts = [n for n in nodes if n.kind is NodeKind.FACT and n.state is NodeState.CONFIRMED][
            -3:
        ]
        if not (hints or open_intents or facts):
            return ""
        lines: list[str] = []
        if hints:
            lines.append("operator hints:")
            lines += [
                f"- {hint.text('text')}" + ("" if hint.state is NodeState.ACTIVE else " (done)")
                for hint in hints
            ]
        if open_intents:
            lines.append("open directions:")
            lines += [
                f"- [{node.state.value}] {node.id}: {node.text()}" for node in open_intents
            ]
        if facts:
            lines.append("latest results:")
            lines += [
                f"- {node.text('summary')[:200]}"
                f"{'' if node.payload.get('artifact_ids') else ' (no artifact)'}"
                for node in facts
            ]
        text = "\n".join(lines)
        return text if len(text) <= FRONTIER_MAX_CHARS else text[:FRONTIER_MAX_CHARS] + "…"

    # --- writing ----------------------------------------------------------

    def origin(self, operation_id: str, task_id: str, title: str) -> GraphNode:
        """The root fact of a task; idempotent."""
        for node in self.graph.for_task(task_id):
            if node.kind is NodeKind.ORIGIN:
                return node
        node = GraphNode(
            operation_id=operation_id,
            task_id=task_id,
            kind=NodeKind.ORIGIN,
            state=NodeState.ORIGIN,
            payload={"title": _clip(title, INTENT_TITLE_MAX)},
            created_by="system",
        )
        self.graph.add(node)
        self._event(operation_id, "graph.origin", node)
        return node

    def open_intent(
        self,
        operation_id: str,
        task_id: str,
        title: str,
        parent_fact_id: str,
        *,
        priority: int = 0,
        created_by: str = "agent",
    ) -> GraphNode:
        title = _clip(title.strip(), INTENT_TITLE_MAX)
        if not title:
            raise GraphInvariantError("an intent needs a title")
        parent = self._require(parent_fact_id, operation_id, task_id)
        confirmed_fact = parent.kind is NodeKind.FACT and parent.state is NodeState.CONFIRMED
        if not confirmed_fact and parent.kind is not NodeKind.ORIGIN:
            raise GraphInvariantError(
                f"intent must hang on a confirmed fact or the task origin, {parent.id} is "
                f"{parent.kind.value}/{parent.state.value}"
            )
        if self.graph.count_intents(task_id) >= MAX_INTENTS_PER_TASK:
            raise GraphLimitError(f"task {task_id} already holds {MAX_INTENTS_PER_TASK} intents")
        if self.graph.count_open_intents(task_id) >= MAX_OPEN_INTENTS:
            raise GraphLimitError(
                f"{MAX_OPEN_INTENTS} intents are already open — close one before opening another"
            )
        node = GraphNode(
            operation_id=operation_id,
            task_id=task_id,
            kind=NodeKind.INTENT,
            state=NodeState.OPEN,
            payload={"title": title},
            priority=priority,
            created_by=created_by,
        )
        self.graph.add(node)
        self.graph.add_edge(operation_id, parent.id, node.id, EdgeRel.SPAWNS.value, _now())
        self.graph.consume_hints(task_id, _now())
        self._event(operation_id, "graph.intent.opened", node)
        return node

    def start_intent(self, node_id: str) -> GraphNode:
        node = self.get(node_id)
        if node.kind is not NodeKind.INTENT:
            raise GraphInvariantError(f"{node.id} is a {node.kind.value}, not an intent")
        return self._move(node, NodeState.RUNNING)

    def close_intent(self, node_id: str, outcome: str, reason: str = "") -> GraphNode:
        """Finish an intent: `done` needs a fact, the other outcomes need a reason."""
        node = self.get(node_id)
        if node.kind is not NodeKind.INTENT:
            raise GraphInvariantError(f"{node.id} is a {node.kind.value}, not an intent")
        try:
            target = NodeState(outcome)
        except ValueError as exc:
            raise GraphInvariantError(f"unknown outcome {outcome!r}") from exc
        if target not in (NodeState.DONE, NodeState.BLOCKED, NodeState.EXHAUSTED):
            raise GraphInvariantError("an intent closes as done, blocked or exhausted")
        if target is NodeState.DONE and not self.graph.targets(node.id, EdgeRel.YIELDS.value):
            raise GraphInvariantError(
                f"intent {node.id} has no fact yet — record one before closing it as done"
            )
        if target is not NodeState.DONE and not reason.strip():
            raise GraphInvariantError(f"closing {node.id} as {target.value} needs a reason")
        return self._move(node, target, reason=reason.strip() or None, completed=True)

    def stop_intent(self, node_id: str, reason: str = "stopped by the operator") -> GraphNode:
        return self.close_intent(node_id, NodeState.BLOCKED.value, reason)

    def reset_intent(self, node_id: str) -> GraphNode:
        node = self.get(node_id)
        if node.state is NodeState.OPEN:
            return node
        return self._move(node, NodeState.OPEN)

    def delete_intent(self, node_id: str, reason: str = "") -> GraphNode:
        node = self.get(node_id)
        return self._move(node, NodeState.DELETED, reason=reason.strip() or "deleted")

    def record_fact(
        self,
        node_id: str,
        summary: str,
        artifact_ids: tuple[str, ...] = (),
        *,
        negative: bool = False,
    ) -> GraphNode:
        """Attach a result to a running intent; artifacts are validated against the operation."""
        intent = self.get(node_id)
        if intent.kind is not NodeKind.INTENT:
            raise GraphInvariantError(f"{intent.id} is a {intent.kind.value}, not an intent")
        if intent.state is not NodeState.RUNNING:
            raise GraphInvariantError(
                f"intent {intent.id} is {intent.state.value}; start it before recording a fact"
            )
        summary = _clip(summary.strip(), FACT_SUMMARY_MAX)
        if not summary:
            raise GraphInvariantError("a fact needs a summary")
        if len(artifact_ids) > MAX_ARTIFACT_REFS:
            raise GraphLimitError(f"at most {MAX_ARTIFACT_REFS} artifacts per fact")
        known = {artifact.id for artifact in self.kernel.kernel.list_artifacts(intent.operation_id)}
        unknown = [artifact_id for artifact_id in artifact_ids if artifact_id not in known]
        if unknown:
            raise GraphInvariantError(f"unknown artifacts: {', '.join(unknown)}")
        node = GraphNode(
            operation_id=intent.operation_id,
            task_id=intent.task_id,
            kind=NodeKind.FACT,
            state=NodeState.CONFIRMED,
            payload={
                "summary": summary,
                "artifact_ids": list(artifact_ids),
                "result": "negative" if negative else "success",
            },
        )
        self.graph.add(node)
        self.graph.add_edge(
            intent.operation_id, intent.id, node.id, EdgeRel.YIELDS.value, _now()
        )
        self._event(intent.operation_id, "graph.fact.recorded", node)
        return node

    def attach_finding(self, fact_id: str, finding_id: str) -> GraphNode:
        """A finding proves a fact, and only a fact with an artifact can carry it."""
        fact = self.get(fact_id)
        if fact.kind is not NodeKind.FACT or fact.state is not NodeState.CONFIRMED:
            raise GraphInvariantError(
                f"a finding hangs on a confirmed fact, {fact.id} is "
                f"{fact.kind.value}/{fact.state.value}"
            )
        if not fact.payload.get("artifact_ids"):
            raise GraphInvariantError(f"fact {fact.id} has no artifact — evidence first")
        node = GraphNode(
            operation_id=fact.operation_id,
            task_id=fact.task_id,
            kind=NodeKind.FINDING,
            state=NodeState.CONFIRMED,
            payload={"finding_id": finding_id},
        )
        self.graph.add(node)
        self.graph.add_edge(
            fact.operation_id, fact.id, node.id, EdgeRel.PROVES.value, _now()
        )
        self._event(fact.operation_id, "graph.finding.attached", node)
        return node

    def add_hint(self, operation_id: str, task_id: str, text: str) -> GraphNode:
        text = _clip(text.strip(), HINT_TEXT_MAX)
        if not text:
            raise GraphInvariantError("a hint needs text")
        node = GraphNode(
            operation_id=operation_id,
            task_id=task_id,
            kind=NodeKind.HINT,
            state=NodeState.ACTIVE,
            payload={"text": text},
            created_by="operator",
        )
        self.graph.add(node)
        self._event(operation_id, "graph.hint.added", node)
        return node

    def dismiss(self, node_id: str) -> GraphNode:
        node = self.get(node_id)
        if node.kind not in (NodeKind.FACT, NodeKind.FINDING):
            raise GraphInvariantError(f"{node.id} is a {node.kind.value}, not a fact or finding")
        dismissed = self.graph.dismiss(node_id, _now())
        if dismissed is None:
            raise GraphConflictError(f"{node.id} is already dismissed")
        self._event(node.operation_id, "graph.dismissed", dismissed)
        return dismissed

    # --- internals --------------------------------------------------------

    def annotate_intent(self, node_id: str, fields: dict[str, str]) -> GraphNode:
        """Attach the next test and the stop condition to a hypothesis.

        They are plan lines, not state: the verdict still needs facts, but a
        hypothesis now says how it could be falsified before the work starts.
        """
        node = self.get(node_id)
        if node.kind is not NodeKind.INTENT:
            raise GraphInvariantError(f"{node.id} is a {node.kind.value}, not an intent")
        payload = dict(node.payload)
        for key, value in fields.items():
            if key in ("next_test", "stop_condition") and value.strip():
                payload[key] = value.strip()[:200]
        self.graph.set_payload(node.id, payload, now=_now())
        self._event(node.operation_id, "graph.intent.annotated", self.get(node.id))
        return self.get(node.id)

    def _move(
        self,
        node: GraphNode,
        target: NodeState,
        *,
        reason: str | None = None,
        completed: bool = False,
    ) -> GraphNode:
        moved = self.graph.transition(
            node.id, node.state, target, reason=reason, completed=completed, now=_now()
        )
        self._event(node.operation_id, "graph.state", moved)
        return moved

    def _require(self, node_id: str, operation_id: str, task_id: str) -> GraphNode:
        node = self.get(node_id)
        if node.operation_id != operation_id:
            raise GraphInvariantError(f"{node_id} belongs to another operation")
        if node.task_id != task_id:
            raise GraphInvariantError(f"{node_id} belongs to another task")
        return node

    def _event(self, operation_id: str, event_type: str, node: GraphNode) -> None:
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=operation_id,
                event_type=event_type,
                payload={"node_id": node.id, "kind": node.kind.value, "state": node.state.value},
            )
        )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
