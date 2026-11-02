"""Local tunnels: policy once, then plain bytes to the target."""

import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from samurai_ai.domain.execution import ToolExecutionError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.tunnel import ScopedTunnel, scope_url


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"tunnel-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def http_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def run_tunnel(tunnel: ScopedTunnel, request: bytes) -> tuple[bytes, list[str]]:
    async def scenario() -> tuple[bytes, list[str]]:
        endpoint = await tunnel.start()
        try:
            reader, writer = await asyncio.open_connection(
                endpoint.host.replace("host.docker.internal", "127.0.0.1"),
                endpoint.port,
            )
            writer.write(request)
            await writer.drain()
            chunks = []
            while chunk := await reader.read(65536):
                chunks.append(chunk)
            writer.close()
            await asyncio.sleep(0.05)
            return b"".join(chunks), [event.verdict for event in tunnel.events]
        finally:
            await tunnel.stop()

    return asyncio.run(scenario())


def make_tunnel(http_server: str, includes: tuple[str, ...]) -> ScopedTunnel:
    port = int(http_server.rsplit(":", 1)[1])
    return ScopedTunnel(
        ScopeContract(includes=includes),
        "127.0.0.1",
        port,
        allowed_ports=frozenset({port}),
    )


def test_a_tunnel_forwards_bytes_and_records_one_allowed_event(
    http_server: str,
) -> None:
    tunnel = make_tunnel(http_server, ("127.0.0.1",))
    body, verdicts = run_tunnel(
        tunnel, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
    )
    assert b"tunnel-ok" in body
    assert verdicts == ["allowed"]
    # volume lands on the tunnel totals, the event is the audit line
    assert tunnel.bytes_sent > 0


def test_a_tunnel_refuses_out_of_scope_before_any_byte(http_server: str) -> None:
    tunnel = make_tunnel(http_server, ("example.com",))
    with pytest.raises(ToolExecutionError, match="denied"):
        asyncio.run(tunnel.start())
    assert [event.verdict for event in tunnel.events] == ["denied-scope"]


def test_a_tunnel_refuses_a_port_outside_the_list(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    tunnel = ScopedTunnel(
        ScopeContract(includes=("127.0.0.1",)),
        "127.0.0.1",
        port,
        allowed_ports=frozenset({port + 1}),
    )
    with pytest.raises(ToolExecutionError, match="denied-port"):
        asyncio.run(tunnel.start())


def test_scope_url_brackets_ipv6() -> None:
    assert scope_url("::1") == "http://[::1]/"
    assert scope_url("example.com") == "http://example.com/"
