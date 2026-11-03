"""Attach to a live session: watch the stream, answer, approve, leave.

The screen owns a socket client on its own thread, so the UI never blocks on the
daemon and the daemon never notices the UI closing.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, RichLog, Static

from samurai_ai.i18n import Translator


class AttachScreen(ModalScreen[None]):
    """Live tail of one operation with the two actions that matter."""

    BINDINGS = [
        Binding("escape", "dismiss", "detach", priority=True),
        Binding("q", "dismiss", "detach", show=False),
        Binding("a", "approve", "approve"),
        Binding("m", "message", "message"),
    ]

    CSS = """
    AttachScreen {
        align: center middle;
    }
    #attach-dialog {
        width: 96%;
        height: 80%;
        padding: 1 2;
        border: solid $accent-alt;
        background: $panel;
    }
    #attach-title {
        text-style: bold;
        color: $accent-alt;
    }
    #attach-hint {
        color: $text-dim;
        padding-bottom: 1;
    }
    #attach-log {
        height: 1fr;
        background: $panel;
    }
    #attach-input {
        display: none;
        margin-top: 1;
    }
    #attach-input.visible {
        display: block;
    }
    """

    def __init__(
        self, translator: Translator, socket_path: Path, operation_id: str
    ) -> None:
        super().__init__()
        self.translator = translator
        self.socket_path = socket_path
        self.operation_id = operation_id
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: Any = None
        self.thread: threading.Thread | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="attach-dialog"):
            yield Static(
                self.translator.t("attach.title"), id="attach-title"
            )
            yield Static(self.translator.t("attach.hint"), id="attach-hint")
            yield RichLog(id="attach-log", wrap=True, markup=False)
            yield Input(id="attach-input", placeholder=self.translator.t("attach.message"))

    # -- lifecycle --------------------------------------------------------
    def on_mount(self) -> None:
        self.thread = threading.Thread(target=self._run_client, daemon=True)
        self.thread.start()

    def _run_client(self) -> None:
        asyncio.run(self._stream())

    async def _stream(self) -> None:
        from samurai_ai.services.rpc.client import Client, RpcFailure

        self.loop = asyncio.get_running_loop()
        try:
            self.client = await Client.connect(
                self.socket_path, client="tui", read_only=False
            )
        except RpcFailure as exc:
            self.app.call_from_thread(self._append, f"error: {exc}")
            return
        self.app.call_from_thread(self._append, self.translator.t("attach.connected"))
        try:
            await self.client.subscribe(self.operation_id, 0)
            async for frame in self.client.stream():
                self.app.call_from_thread(self._append, self._format(frame))
        finally:
            await self.client.close()

    def _format(self, frame: dict[str, Any]) -> str:
        payload = frame.get("payload") or {}
        detail = ", ".join(f"{key}={value}" for key, value in sorted(payload.items()))
        return f"[{frame.get('seq')}] {frame.get('event')} {detail}"[:300]

    def _append(self, line: str) -> None:
        self.query_one("#attach-log", RichLog).write(line)

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        if self.loop is None or self.client is None:
            return None
        future = asyncio.run_coroutine_threadsafe(self.client.call(method, params), self.loop)
        return future.result(timeout=10)

    # -- actions ----------------------------------------------------------
    def action_approve(self) -> None:
        try:
            pending = self._call("approvals.list", {"operation_id": self.operation_id})
        except Exception as exc:  # noqa: BLE001 - shown, not raised
            self._append(f"error: {exc}")
            return
        if not pending:
            self._append(self.translator.t("attach.nothing-pending"))
            return
        try:
            self._call("approvals.decide", {"approval_id": pending[0]["id"]})
        except Exception as exc:  # noqa: BLE001
            self._append(f"error: {exc}")
            return
        self._append(self.translator.t("attach.approved").format(action=pending[0]["action"]))

    def action_message(self) -> None:
        field = self.query_one("#attach-input", Input)
        field.add_class("visible")
        field.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        try:
            self._call(
                "send.message", {"operation_id": self.operation_id, "message": text}
            )
        except Exception as exc:  # noqa: BLE001
            self._append(f"error: {exc}")
        else:
            self._append(f"> {text}")
        event.input.value = ""
        event.input.remove_class("visible")

    async def action_dismiss(self, result: None = None) -> None:
        if self.client is not None and self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.client.close(), self.loop)
        self.dismiss(None)
