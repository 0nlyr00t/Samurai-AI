"""Plan and findings boards: rows, actions, detail sections.

The screens are dumb (see ``tui/screens/board.py``); everything that needs a
service lives here. Every action goes through the kernel, so a board edit is as
governed as the same edit from the CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from samurai_ai.domain.kernel import FindingStatus, GateStatus, TaskStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from samurai_ai.domain.kernel import Finding, OperationPlan, PlanTask, StageGate
    from samurai_ai.domain.operations import Operation

Row = tuple[str, str, str]

_TASK_OPEN = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
_TASK_CLOSED = (TaskStatus.DONE, TaskStatus.SKIPPED)
_FINDING_OPEN = (FindingStatus.CANDIDATE, FindingStatus.VALIDATING)
_FINDING_VALIDATED = (FindingStatus.VALIDATED,)
_FINDING_CLOSED = (
    FindingStatus.FALSE_POSITIVE,
    FindingStatus.DUPLICATE,
    FindingStatus.REPORTED,
)


class BoardController:
    """Mixin: the two boards and their actions."""

    if TYPE_CHECKING:  # pragma: no cover - typing only
        operation: Operation | None
        kernel: Any
        execution: Any
        orchestrator: Any
        translator: Any

        def child_runs(self) -> list[Any]: ...

        def _write_line(self, message: Any) -> None: ...
        def _write_dim(self, message: str) -> None: ...
        def _refresh_chrome(self) -> None: ...
        def push_screen(self, screen: Any, callback: Any = None) -> Any: ...

        push_screen_wait: Any

    # -- plan board -------------------------------------------------------
    def _task_rows(self, show_all: bool) -> list[Row]:
        plan = self._current_plan()
        if plan is None:
            return []
        rows: list[Row] = []
        open_tasks = [task for task in plan.tasks if task.status in _TASK_OPEN]
        closed_tasks = [task for task in plan.tasks if task.status in _TASK_CLOSED]
        if open_tasks:
            rows.append(("group", self.translator.t("board.group.open"), ""))
            rows.extend(self._task_row(task) for task in open_tasks)
        if closed_tasks and (show_all or not open_tasks):
            rows.append(("group", self.translator.t("board.group.done"), ""))
            rows.extend(self._task_row(task) for task in closed_tasks)
        gates = [gate for gate in plan.gates if gate.status is not GateStatus.PASSED]
        if gates:
            rows.append(("group", self.translator.t("board.group.gates"), ""))
            rows.extend(self._gate_row(gate) for gate in gates)
        return rows

    def _task_row(self, task: PlanTask) -> Row:
        deps = f" ← {', '.join(task.depends_on)}" if task.depends_on else ""
        risks = f" r{task.risk_class.value[-1]}" if task.risk_class else ""
        return (
            "item",
            task.id,
            f"{task.id} [{task.status.value}]{risks} {task.title}{deps}",
        )

    def _gate_row(self, gate: StageGate) -> Row:
        return ("item", f"gate:{gate.id}", f"{gate.id} [{gate.status.value}] {gate.name}")

    def _show_task_board(self) -> None:
        if not self._require_operation():
            return
        from samurai_ai.tui.screens import BoardScreen

        actions: dict[str, Any] = {
            "s": self._task_start,
            "d": self._task_done,
            "k": self._task_skip,
            "b": self._task_block,
            "p": self._task_reset,
            "c": self._task_spawn,
            "g": self._gate_pass,
        }
        labels = {key: self.translator.t(f"board.action.{key}") for key in actions}
        self.push_screen(
            BoardScreen(
                self.translator,
                self.translator.t("board.plan-title"),
                self.translator.t(
                    "board.hint",
                    actions=" · ".join(f"{key.upper()} {label}" for key, label in labels.items()),
                ),
                self._task_rows,
                actions,
                labels,
                self._show_task_detail,
                self.translator.t("board.no-plan"),
            )
        )

    # -- finding board ----------------------------------------------------
    def _finding_rows(self, show_all: bool) -> list[Row]:
        findings = self._findings()
        if not findings:
            return []
        rows: list[Row] = []
        buckets = (
            ("board.group.open", _FINDING_OPEN),
            ("board.group.validated", _FINDING_VALIDATED),
            ("board.group.closed", _FINDING_CLOSED),
        )
        for key, statuses in buckets:
            group = [item for item in findings if item.status in statuses]
            if not group:
                continue
            if not show_all and statuses is _FINDING_CLOSED:
                continue
            rows.append(("group", self.translator.t(key), ""))
            for item in group:
                rows.append(
                    (
                        "item",
                        item.id,
                        f"{item.id} [{item.severity.value}/{item.status.value}] {item.title}",
                    )
                )
        return rows

    def _show_finding_board(self) -> None:
        if not self._require_operation():
            return
        from samurai_ai.tui.screens import BoardScreen

        actions: dict[str, Any] = {
            "v": self._finding_validate,
            "x": self._finding_false_positive,
            "u": self._finding_duplicate,
            "r": self._finding_reported,
            "e": self._finding_evidence,
        }
        labels = {key: self.translator.t(f"board.action.{key}") for key in actions}
        self.push_screen(
            BoardScreen(
                self.translator,
                self.translator.t("board.finding-title"),
                self.translator.t(
                    "board.hint",
                    actions=" · ".join(f"{key.upper()} {label}" for key, label in labels.items()),
                ),
                self._finding_rows,
                actions,
                labels,
                self._show_finding_detail,
                self.translator.t("board.no-findings"),
            )
        )

    # -- task actions -----------------------------------------------------
    def _task_by_id(self, task_id: str) -> PlanTask | None:
        plan = self._current_plan()
        return plan.task_by_id(task_id) if plan is not None else None

    def _plan_action(self, what: str, target: str, call: Any) -> None:
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - a board action must not crash
            self._write_line(f"[board] {exc}")
            return
        self._write_dim(self.translator.t("board.done", action=what, item=target))
        self._refresh_chrome()

    def _task_start(self, task_id: str) -> None:
        self._plan_action(
            self.translator.t("board.action.s"),
            task_id,
            lambda: self.kernel.start_task(self.operation.id, task_id),  # type: ignore[union-attr]
        )

    def _task_done(self, task_id: str) -> None:
        self._plan_action(
            self.translator.t("board.action.d"),
            task_id,
            lambda: self.kernel.complete_task(self.operation.id, task_id),  # type: ignore[union-attr]
        )

    def _task_skip(self, task_id: str) -> None:
        self._plan_action(
            self.translator.t("board.action.k"),
            task_id,
            lambda: self.kernel.skip_task(self.operation.id, task_id),  # type: ignore[union-attr]
        )

    def _task_block(self, task_id: str) -> None:
        self._plan_action(
            self.translator.t("board.action.b"),
            task_id,
            lambda: self.kernel.block_task(self.operation.id, task_id),  # type: ignore[union-attr]
        )

    def _task_reset(self, task_id: str) -> None:
        self._plan_action(
            self.translator.t("board.action.p"),
            task_id,
            lambda: self.kernel.reset_task(self.operation.id, task_id),  # type: ignore[union-attr]
        )

    def _task_spawn(self, task_id: str) -> None:
        task = self._task_by_id(task_id)
        if task is None:
            return
        self.run_worker(self.spawn_subagent(task.title), exclusive=False)  # type: ignore[attr-defined]

    def _gate_pass(self, row_id: str) -> None:
        if not row_id.startswith("gate:"):
            return
        gate_id = row_id.split(":", 1)[1]
        self._plan_action(
            self.translator.t("board.action.g"),
            gate_id,
            lambda: self.kernel.pass_gate(self.operation.id, gate_id),  # type: ignore[union-attr]
        )

    # -- finding actions --------------------------------------------------
    def _finding_action(self, what: str, finding_id: str, call: Any) -> None:
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - a board action must not crash
            self._write_line(f"[board] {exc}")
            return
        self._write_dim(self.translator.t("board.done", action=what, item=finding_id))
        self._refresh_chrome()

    def _finding_validate(self, finding_id: str) -> None:
        if not self._evidence_ids(finding_id):
            self._write_line(self.translator.t("board.needs-evidence", finding=finding_id))
            return
        self._finding_action(
            self.translator.t("board.action.v"),
            finding_id,
            lambda: self.kernel.validate_finding(finding_id),
        )

    def _finding_false_positive(self, finding_id: str) -> None:
        self._finding_action(
            self.translator.t("board.action.x"),
            finding_id,
            lambda: self.kernel.mark_false_positive(finding_id),
        )

    def _finding_reported(self, finding_id: str) -> None:
        self._finding_action(
            self.translator.t("board.action.r"),
            finding_id,
            lambda: self.kernel.mark_reported(finding_id),
        )

    def _finding_duplicate(self, finding_id: str) -> None:
        """Ask which finding this one duplicates, then mark it."""

        async def ask() -> None:
            from samurai_ai.tui.screens import ArgumentScreen

            candidates = [
                item.id
                for item in self._findings()
                if item.id != finding_id and item.status not in _FINDING_CLOSED
            ]
            label = self.translator.t("board.duplicate-of", candidates=", ".join(candidates) or "—")
            original = await self.push_screen_wait(ArgumentScreen(self.translator, label))
            if not original or not original.strip():
                return
            self._finding_action(
                self.translator.t("board.action.u"),
                finding_id,
                lambda: self.kernel.mark_duplicate(finding_id, original.strip()),
            )

        self.run_worker(ask(), exclusive=False)  # type: ignore[attr-defined]

    def _finding_evidence(self, finding_id: str) -> None:
        """Attach an evidence artifact, chosen from the operation's artifacts."""

        async def ask() -> None:
            from samurai_ai.tui.screens import ArgumentScreen

            artifacts = [
                artifact.id
                for artifact in self.kernel.kernel.list_artifacts(
                    self.operation.id  # type: ignore[union-attr]
                )
                if artifact.id not in self._evidence_ids(finding_id)
            ]
            if not artifacts:
                self._write_line(self.translator.t("board.no-artifacts"))
                return
            label = self.translator.t("board.evidence-of", candidates=", ".join(artifacts[-8:]))
            artifact_id = await self.push_screen_wait(ArgumentScreen(self.translator, label))
            if not artifact_id or not artifact_id.strip():
                return
            self._finding_action(
                self.translator.t("board.action.e"),
                finding_id,
                lambda: self.kernel.attach_evidence(finding_id, artifact_id.strip()),
            )

        self.run_worker(ask(), exclusive=False)  # type: ignore[attr-defined]

    def _evidence_ids(self, finding_id: str) -> list[str]:
        finding = self.kernel.kernel.get_finding(finding_id)
        return list(finding.evidence_ids) if finding is not None else []

    # -- details ----------------------------------------------------------
    def _show_task_detail(self, task_id: str) -> None:
        from samurai_ai.tui.screens import ItemDetailScreen

        task = self._task_by_id(task_id)
        if task is None:
            return
        actions: dict[str, Any] = {
            "s": lambda: self._task_start(task_id),
            "d": lambda: self._task_done(task_id),
            "k": lambda: self._task_skip(task_id),
            "b": lambda: self._task_block(task_id),
            "p": lambda: self._task_reset(task_id),
            "c": lambda: self._task_spawn(task_id),
        }
        self.push_screen(
            ItemDetailScreen(
                self.translator,
                self.translator.t("board.task-title", task=task.id),
                self.translator.t(
                    "board.detail-hint",
                    actions=" · ".join(
                        f"{key.upper()} {self.translator.t(f'board.action.{key}')}"
                        for key in actions
                    ),
                ),
                self._task_sections(task),
                actions,
            )
        )

    def _task_sections(self, task: PlanTask) -> list[tuple[str, list[str]]]:
        t = self.translator
        runs = [
            run.id
            for run in self.execution.list_runs(self.operation.id)  # type: ignore[union-attr]
            if run.task_id == task.id
        ]
        children = [
            run.id
            for run in self.child_runs()
            if run.task_id == task.id
        ]
        return [
            (
                t.t("board.section.state"),
                [
                    t.t("board.task-state", status=task.status.value),
                    t.t("board.task-risk", risk=task.risk_class.value),
                ],
            ),
            (
                t.t("board.section.text"),
                [task.title, task.description] if task.description else [task.title],
            ),
            (t.t("board.section.deps"), self._dep_lines(task)),
            (t.t("board.section.requires"), list(task.required_artifacts)),
            (t.t("board.section.runs"), runs),
            (t.t("board.section.children"), children),
        ]

    def _dep_lines(self, task: PlanTask) -> list[str]:
        plan = self._current_plan()
        lines: list[str] = []
        for dep_id in task.depends_on:
            other = plan.task_by_id(dep_id) if plan is not None else None
            lines.append(f"{dep_id} — {other.title}" if other is not None else dep_id)
        return lines

    def _show_finding_detail(self, finding_id: str) -> None:
        from samurai_ai.tui.screens import ItemDetailScreen

        finding = self.kernel.kernel.get_finding(finding_id)
        if finding is None:
            return
        actions: dict[str, Any] = {
            "v": lambda: self._finding_validate(finding_id),
            "x": lambda: self._finding_false_positive(finding_id),
            "u": lambda: self._finding_duplicate(finding_id),
            "r": lambda: self._finding_reported(finding_id),
            "e": lambda: self._finding_evidence(finding_id),
        }
        self.push_screen(
            ItemDetailScreen(
                self.translator,
                self.translator.t("board.finding-title-one", finding=finding.id),
                self.translator.t(
                    "board.detail-hint",
                    actions=" · ".join(
                        f"{key.upper()} {self.translator.t(f'board.action.{key}')}"
                        for key in actions
                    ),
                ),
                self._finding_sections(finding),
                actions,
            )
        )

    def _finding_sections(self, finding: Finding) -> list[tuple[str, list[str]]]:
        t = self.translator
        return [
            (
                t.t("board.section.state"),
                [
                    t.t(
                        "board.finding-state",
                        severity=finding.severity.value,
                        status=finding.status.value,
                    )
                ],
            ),
            (
                t.t("board.section.text"),
                [finding.title, finding.summary] if finding.summary else [finding.title],
            ),
            (
                t.t("board.section.remediation"),
                [finding.remediation] if finding.remediation else [],
            ),
            (t.t("board.section.evidence"), list(finding.evidence_ids)),
            (
                t.t("board.section.duplicate"),
                [finding.duplicate_of] if finding.duplicate_of else [],
            ),
        ]

    # -- helpers ----------------------------------------------------------
    def _require_operation(self) -> bool:
        if self.operation is None:
            self._write_line("[board] " + self.translator.t("status.no-operation"))
            return False
        return True

    def _current_plan(self) -> OperationPlan | None:
        if self.operation is None:
            return None
        plan: OperationPlan | None = self.kernel.get_current_plan(self.operation.id)
        return plan

    def _findings(self) -> list[Finding]:
        if self.operation is None:
            return []
        findings: list[Finding] = self.kernel.kernel.list_findings(self.operation.id)
        return findings
