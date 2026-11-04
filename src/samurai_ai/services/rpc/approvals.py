"""Waiting for an operator's decision instead of failing the run.

An unattended run used to need ``--auto-approve`` or a pre-approved id. With a
daemon someone can be attached, so the run asks and waits — bounded, because a
detached run must not hang forever.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

DEFAULT_WAIT_SECONDS = 300.0
POLL_SECONDS = 0.2


class ApprovalWaiter:
    def __init__(
        self,
        pending: Callable[[str], bool],
        *,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.pending = pending
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds

    def __call__(self, approval_id: str) -> bool:
        waited = 0.0
        while waited < self.wait_seconds:
            if not self.pending(approval_id):
                return True
            stop = threading.Event()
            stop.wait(self.poll_seconds)
            waited += self.poll_seconds
        return False


__all__ = ["ApprovalWaiter"]
