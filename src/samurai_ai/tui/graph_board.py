"""The graph board: see what the model sees, and steer it.

The board reads the same nodes the prompt frontier is built from, so the screen
and the model cannot drift apart. Actions are the operator's alone — stop, reset,
delete an intent, dismiss a fact, drop a hint — and each one goes through the
graph service so the invariants hold here too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from samurai_ai.domain.graph import GraphError, GraphNode, NodeKind, NodeState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from samurai_ai.domain.operations import Operation
    from samurai_ai.services.graph import GraphService

Row = tuple[str, str, str]

#: Group header, the kind it lists and the states that belong there. The kind
#: matters: facts and findings share the `confirmed` state.
_GROUPS: tuple[tuple[str, NodeKind, frozenset[NodeState]], ...] = (
    (
        "board.group.intents",
        NodeKind.INTENT,
        frozenset({NodeState.OPEN, NodeState.RUNNING, NodeState.PAUSED}),
    ),
    ("board.group.hints", NodeKind.HINT, frozenset({NodeState.ACTIVE, NodeState.CONSUMED})),
    ("board.group.facts", NodeKind.FACT, frozenset({NodeState.CONFIRMED})),
    ("board.group.findings", NodeKind.FINDING, frozenset({NodeState.CONFIRMED})),
)
_CLOSED_STATES = frozenset(
    {
        NodeState.DONE,
        NodeState.BLOCKED,
        NodeState.EXHAUSTED,
        NodeState.DELETED,
        NodeState.DISMISSED,
    }
)


class GraphBoardController:
    """Mixin: the graph board and its actions."""

    if TYPE_CHECKING:  # pragma: no cover - typing only
        operation: Operation | None
        graph: GraphService
        translator: Any

        def _write_line(self, message: Any) -> None: ...
        def _write_dim(self, message: str, *, process: bool = True) -> None: ...
        def _refresh_chrome(self) -> None: ...
        def run_worker(self, *args: Any, **kwargs: Any) -> Any: ...
        def push_screen(self, screen: Any, callback: Any = None) -> Any: ...
        push_screen_wait: Any

    # -- rows -------------------------------------------------------------
    def _graph_nodes(self) -> list[GraphNode]:
        if self.operation is None:
            return []
        return self.graph.for_operation(self.operation.id)

    def _graph_rows(self, show_all: bool) -> list[Row]:
        nodes = self._graph_nodes()
        if not nodes:
            return []
        rows: list[Row] = []
        for key, kind, states in _GROUPS:
            group = [node for node in nodes if node.kind is kind and node.state in states]
            if not group:
                continue
            rows.append(("group", self.translator.t(key), ""))
            rows.extend(self._graph_row(node) for node in group)
        if show_all:
            closed = [node for node in nodes if node.state in _CLOSED_STATES]
            if closed:
                rows.append(("group", self.translator.t("board.group.closed"), ""))
                rows.extend(self._graph_row(node) for node in closed)
        return rows

    def _graph_row(self, node: GraphNode) -> Row:
        what = node.text("title") or node.text("summary") or node.text("text")
        mark = ""
        if node.kind is NodeKind.FACT and not node.payload.get("artifact_ids"):
            mark = " " + self.translator.t("board.graph-bare")
        if node.blocked_reason:
            mark += f" — {node.blocked_reason}"
        return ("item", node.id, f"{node.id} [{node.state.value}] {what[:90]}{mark}")

    # -- screen -----------------------------------------------------------
    def _graph_label(self, key: str) -> str:
        """Graph actions share keys with the other boards but mean different things."""
        return str(self.translator.t(f"board.graph-action.{key}"))

    def _show_graph_board(self) -> None:
        if self.operation is None:
            self._write_line("[error] " + self.translator.t("palette.no-operation"))
            return
        from samurai_ai.tui.screens import BoardScreen

        actions: dict[str, Any] = {
            "s": self._graph_stop,
            "p": self._graph_reset,
            "x": self._graph_delete,
            "d": self._graph_dismiss,
            "n": self._graph_hint,
        }
        labels = {key: self._graph_label(key) for key in actions}
        self.push_screen(
            BoardScreen(
                self.translator,
                self.translator.t("board.graph-title"),
                self.translator.t(
                    "board.hint",
                    actions=" · ".join(f"{key.upper()} {label}" for key, label in labels.items()),
                ),
                self._graph_rows,
                actions,
                labels,
                self._show_graph_detail,
                self.translator.t("board.no-graph"),
            )
        )

    def _show_graph_detail(self, node_id: str) -> None:
        from samurai_ai.tui.screens import ItemDetailScreen

        node = self.graph.get(node_id)
        actions: dict[str, Any] = {
            "s": lambda: self._graph_stop(node_id),
            "p": lambda: self._graph_reset(node_id),
            "x": lambda: self._graph_delete(node_id),
        }
        if node.kind in (NodeKind.FACT, NodeKind.FINDING):
            actions["d"] = lambda: self._graph_dismiss(node_id)
        self.push_screen(
            ItemDetailScreen(
                self.translator,
                self.translator.t("board.graph-title-one", node=node.id),
                self.translator.t(
                    "board.detail-hint",
                    actions=" · ".join(
                        f"{key.upper()} {self._graph_label(key)}" for key in actions
                    ),
                ),
                self._graph_sections(node),
                actions,
            )
        )

    def _graph_sections(self, node: GraphNode) -> list[tuple[str, list[str]]]:
        sections: list[tuple[str, list[str]]] = [
            (self.translator.t("board.section.graph-node"), [f"{node.kind.value}: {node.id}"]),
            (self.translator.t("board.section.graph-state"), [node.state.value]),
        ]
        text = node.text("title") or node.text("summary") or node.text("text")
        if text:
            sections.append((self.translator.t("board.section.graph-text"), [text]))
        artifacts = cast("list[str]", node.payload.get("artifact_ids") or [])
        if artifacts:
            sections.append(
                (self.translator.t("board.section.graph-artifacts"), list(artifacts))
            )
        lineage = self.graph.lineage(node.id)
        if lineage:
            sections.append(
                (
                    self.translator.t("board.section.graph-lineage"),
                    [f"{parent.kind.value} {parent.id}: {parent.text()}" for parent in lineage],
                )
            )
        if node.blocked_reason:
            sections.append(
                (self.translator.t("board.section.graph-reason"), [node.blocked_reason])
            )
        return sections

    # -- actions ----------------------------------------------------------
    def _graph_action(self, what: str, node_id: str, call: Any) -> None:
        try:
            call()
        except GraphError as exc:
            self._write_line(f"[graph] {exc}")
            return
        self._write_dim(self.translator.t("board.done", action=what, item=node_id))
        self._refresh_chrome()

    def _graph_stop(self, node_id: str) -> None:
        self._graph_action(
            self._graph_label("s"),
            node_id,
            lambda: self.graph.stop_intent(node_id),
        )

    def _graph_reset(self, node_id: str) -> None:
        self._graph_action(
            self._graph_label("p"),
            node_id,
            lambda: self.graph.reset_intent(node_id),
        )

    def _graph_delete(self, node_id: str) -> None:
        self._graph_action(
            self._graph_label("x"),
            node_id,
            lambda: self.graph.delete_intent(node_id),
        )

    def _graph_dismiss(self, node_id: str) -> None:
        self._graph_action(
            self._graph_label("d"),
            node_id,
            lambda: self.graph.dismiss(node_id),
        )

    def _graph_hint(self, node_id: str) -> None:
        """Ask for the hint text and hang it on the node's task."""
        node = self.graph.get(node_id)
        self.run_worker(self._ask_hint(node), exclusive=False)

    async def _ask_hint(self, node: GraphNode) -> None:
        from samurai_ai.tui.screens import ArgumentScreen

        answer = await self.push_screen_wait(
            ArgumentScreen(self.translator, self.translator.t("board.graph-hint-prompt"))
        )
        if not answer or self.operation is None:
            return
        self._graph_action(
            self._graph_label("n"),
            node.task_id,
            lambda: self.graph.add_hint(self.operation.id, node.task_id, answer),
        )
