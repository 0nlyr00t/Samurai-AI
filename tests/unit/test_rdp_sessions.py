from __future__ import annotations

import asyncio
from pathlib import Path

from test_sessions import FakeProcessManager

from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessResult
from samurai_ai.execution.remote import RdpExecTool
from samurai_ai.services.rdp_sessions import RdpBackend
from samurai_ai.services.sessions import CommandStatus, SessionService


async def _ready(control_dir: str) -> None:
    await asyncio.to_thread(Path(control_dir, "ready.json").write_text, "{}")


def test_rdp_backend_reuses_master_and_reconnects_without_replay(tmp_path: Path) -> None:
    manager = FakeProcessManager()
    backend = RdpBackend(manager)
    backend._wait_ready = _ready  # type: ignore[assignment]
    service = SessionService(manager=manager, backend=backend)

    async def exercise() -> None:
        session = await service.open(
            "op-1",
            ScopeContract(includes=("10.0.0.5",)),
            "10.0.0.5",
            "administrator",
            password="secret",
            port=3389,
            metadata={"endpoint": "127.0.0.1:4444"},
        )
        first = await service.exec(session.id, "id")
        await service.exec(session.id, "whoami")
        assert len(manager.started) == 3  # one desktop master + two request drivers
        await asyncio.to_thread(Path(session.socket_path + ".rdp", "ready.json").unlink)
        await service.reconnect(session.id)
        assert service.get_command(session.id, first.id).status is CommandStatus.UNKNOWN
        assert len(manager.started) == 4  # replacement master, not a replay
        assert any("127.0.0.1:4444" in argv for argv in manager.started)
        await service.close(session.id)

    asyncio.run(exercise())


def test_rdp_exec_tool_uses_stateful_backend_and_scoped_endpoint() -> None:
    manager = FakeProcessManager()
    manager.results["proc_2"] = ProcessResult(
        exit_code=0, stdout="uid=0(root)\n", stderr=""
    )
    tool = RdpExecTool(session_manager=manager)
    backend = RdpBackend(manager)
    backend._wait_ready = _ready  # type: ignore[assignment]

    class Tunnel:
        port = 4444
        events = []

        async def stop(self) -> None:
            return None

    tool._open = lambda _params, _scope: _tunnel()  # type: ignore[assignment]

    async def _tunnel():
        return Tunnel()

    # build the tool's service through the same RdpBackend test double
    from samurai_ai.services.sessions import SessionService

    original = SessionService.open

    async def open_with_backend(self, *args, **kwargs):
        kwargs["backend"] = backend
        return await original(self, *args, **kwargs)

    SessionService.open = open_with_backend  # type: ignore[assignment]
    try:
        result = tool.invoke(
            {
                "host": "10.0.0.5",
                "user": "administrator",
                "password": "secret",
                "command": "id",
            },
            ScopeContract(includes=("10.0.0.5",)),
        )
    finally:
        SessionService.open = original  # type: ignore[assignment]

    assert result.stdout == "uid=0(root)\n"
    assert result.exit_code == 0
    assert any("127.0.0.1:4444" in argv for argv in manager.started)
    assert any("python3" in argv for argv in manager.started)


def test_rdp_http_fetch_is_bounded_and_http_only() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from samurai_ai.services.rdp_sessions import rdp_http_fetch

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = rdp_http_fetch(f"http://127.0.0.1:{server.server_port}/")
        assert (status, body) == (200, "ok")
        try:
            rdp_http_fetch("file:///etc/passwd")
        except ValueError as exc:
            assert "http(s)" in str(exc)
        else:
            raise AssertionError("non-http URL accepted")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_rdp_exec_timeout_is_unknown() -> None:
    manager = FakeProcessManager()
    manager.results["proc_2"] = ProcessResult(
        exit_code=None, stdout="partial", stderr="", timed_out=True
    )
    tool = RdpExecTool(session_manager=manager)
    backend = RdpBackend(manager)
    backend._wait_ready = _ready  # type: ignore[assignment]

    class Tunnel:
        port = 4444
        events = []

        async def stop(self) -> None:
            return None

    async def _open(_params, _scope):
        return Tunnel()

    tool._open = _open  # type: ignore[assignment]
    from samurai_ai.services.sessions import SessionService

    original = SessionService.open

    async def open_with_backend(self, *args, **kwargs):
        kwargs["backend"] = backend
        return await original(self, *args, **kwargs)

    SessionService.open = open_with_backend  # type: ignore[assignment]
    try:
        result = tool.invoke(
            {
                "host": "10.0.0.5",
                "user": "administrator",
                "password": "secret",
                "command": "id",
            },
            ScopeContract(includes=("10.0.0.5",)),
        )
    finally:
        SessionService.open = original  # type: ignore[assignment]

    assert result.unknown is True
