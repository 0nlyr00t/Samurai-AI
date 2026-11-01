"""The exploration graph: why, what came out, and what proves it.

A task is the unit of work; inside it an *intent* names one direction, a *fact*
records what the intent produced, and a *finding* proves a fact. An operator
*hint* steers from above. The states and the invariants live here so storage and
service agree, and `md/exploration-graph.md` is the design this encodes.

The graph is not evidence on its own: a fact is only worth something when it
carries an artifact, which is why the service refuses facts and findings without
one instead of trusting the summary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

#: Caps, checked by the service — never silently truncated.
INTENT_TITLE_MAX = 200
FACT_SUMMARY_MAX = 2000
HINT_TEXT_MAX = 500
MAX_ARTIFACT_REFS = 8
MAX_INTENTS_PER_TASK = 40
MAX_OPEN_INTENTS = 8
#: The prompt block budget for the whole frontier.
FRONTIER_MAX_CHARS = 1200

RUN_PREFIX = "run."


class GraphError(ValueError):
    pass


class GraphInvariantError(GraphError):
    """A transition that the design forbids, with a reason a model can act on."""


class GraphLimitError(GraphError):
    pass


class GraphNotFoundError(LookupError):
    pass


class GraphConflictError(GraphError):
    """The node moved while the caller held an older state."""


class NodeKind(StrEnum):
    ORIGIN = "origin"
    INTENT = "intent"
    FACT = "fact"
    FINDING = "finding"
    HINT = "hint"


class NodeState(StrEnum):
    ORIGIN = "origin"
    OPEN = "open"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"
    DELETED = "deleted"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    ACTIVE = "active"
    CONSUMED = "consumed"


class EdgeRel(StrEnum):
    SPAWNS = "spawns"
    YIELDS = "yields"
    PROVES = "proves"
    DERIVED_FROM = "derived_from"


#: Which states each kind may hold. Anything else is rejected on write.
ALLOWED_STATES: dict[NodeKind, frozenset[NodeState]] = {
    NodeKind.ORIGIN: frozenset({NodeState.ORIGIN}),
    NodeKind.INTENT: frozenset(
        {
            NodeState.OPEN,
            NodeState.RUNNING,
            NodeState.PAUSED,
            NodeState.DONE,
            NodeState.BLOCKED,
            NodeState.EXHAUSTED,
            NodeState.DELETED,
        }
    ),
    NodeKind.FACT: frozenset({NodeState.CONFIRMED, NodeState.DISMISSED}),
    NodeKind.FINDING: frozenset({NodeState.CONFIRMED, NodeState.DISMISSED}),
    NodeKind.HINT: frozenset({NodeState.ACTIVE, NodeState.CONSUMED}),
}

#: Intents in these states keep a task open.
OPEN_INTENT_STATES = frozenset({NodeState.OPEN, NodeState.RUNNING, NodeState.PAUSED})
#: Intents that may still take work.
CLAIMABLE_INTENT_STATES = frozenset({NodeState.OPEN})


class GraphNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"node_{uuid4().hex[:12]}")
    operation_id: str
    task_id: str
    kind: NodeKind
    state: NodeState
    payload: dict[str, object] = Field(default_factory=dict)
    priority: int = 0
    blocked_reason: str | None = None
    delete_reason: str | None = None
    created_by: str = "agent"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    def text(self, key: str = "title") -> str:
        value = self.payload.get(key, "")
        return value if isinstance(value, str) else ""

    @property
    def open(self) -> bool:
        return self.kind is NodeKind.INTENT and self.state in OPEN_INTENT_STATES
