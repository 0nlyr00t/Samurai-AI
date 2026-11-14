"""The collapse check is about *this* turn, not the operation's whole history.

`_completion_reason` read the global finding count, so one old finding from an
earlier turn masked a turn whose every run collapsed. The baseline is now the
count at turn start: only a finding produced while the turn ran counts.
"""

from __future__ import annotations

from typing import Any

from samurai_ai.i18n import Translator
from samurai_ai.tui.chat import ChatController
from samurai_ai.tui.progress import TerminalReason


class _Translator(Translator):
    def __init__(self) -> None:  # noqa: D107 - a stub, not the real catalog
        self.written: list[str] = []

    def t(self, key: str, **_: Any) -> str:
        return key


def _controller(*, now: dict[str, int], findings: int) -> ChatController:
    stand = object.__new__(ChatController)
    stand.translator = _Translator()  # type: ignore[assignment]
    written: list[str] = []

    stand._run_outcome_counts = lambda _op: dict(now)  # type: ignore[method-assign]
    stand._finding_count = lambda: findings  # type: ignore[method-assign]
    stand._write_dim = (  # type: ignore[method-assign]
        lambda text, process=True: written.append(text)  # noqa: ARG005
    )
    stand_written = written  # type: ignore[attr-defined]
    stand.written = stand_written  # type: ignore[attr-defined]
    return stand


def test_a_previous_finding_does_not_mask_a_collapsed_turn() -> None:
    stand = _controller(now={"failed": 2, "completed": 0, "timeout": 1}, findings=1)
    reason = stand._completion_reason("op_1", {"failed": 0, "completed": 0, "timeout": 0}, 1)
    assert reason is TerminalReason.BLOCKED
    assert stand.written  # the operator sees why


def test_a_new_finding_in_this_turn_allows_completed() -> None:
    stand = _controller(now={"failed": 2, "completed": 0, "timeout": 0}, findings=2)
    reason = stand._completion_reason("op_1", {"failed": 0, "completed": 0, "timeout": 0}, 1)
    assert reason is TerminalReason.COMPLETED


def test_a_successful_run_in_this_turn_allows_completed() -> None:
    stand = _controller(now={"failed": 1, "completed": 1, "timeout": 0}, findings=1)
    reason = stand._completion_reason("op_1", {"failed": 0, "completed": 0, "timeout": 0}, 1)
    assert reason is TerminalReason.COMPLETED


def test_a_turn_without_runs_stays_completed() -> None:
    stand = _controller(now={"failed": 1, "completed": 0, "timeout": 0}, findings=1)
    reason = stand._completion_reason("op_1", {"failed": 1, "completed": 0, "timeout": 0}, 1)
    assert reason is TerminalReason.COMPLETED
