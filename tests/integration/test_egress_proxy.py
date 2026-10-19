import asyncio
import http.client
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.domain.execution import ApprovalRequiredError, ToolRunStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.egress import EgressProxy, generate_proxy_token
from samurai_ai.execution.process import ProcessResult
from samurai_ai.execution.registry import SandboxExecTool, ToolRegistry
from samurai_ai.execution.sandbox import DockerSandboxRunner, sign_allowlist
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository

TOKEN = "cd" * 16
MASTER_KEY = bytes.fromhex("ab" * 32)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"egress-ok"
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


def run_proxy(proxy: EgressProxy, requests: list[dict]) -> None:
    asyncio.run(_run_proxy(proxy, requests))


async def _run_proxy(proxy: EgressProxy, requests: list[dict]) -> None:
    host, port = await proxy.start()
    try:
        for request in requests:
            await asyncio.to_thread(_client_request, port, request)
    finally:
        await proxy.stop()


def _client_request(proxy_port: int, request: dict) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    headers = {"Host": request["host_header"]}
    if request.get("token") is not None:
        headers["Proxy-Authorization"] = f"Bearer {request['token']}"
    connection.request("GET", request["target"], headers=headers)
    response = connection.getresponse()
    request["status"] = response.status
    request["body"] = response.read()
    connection.close()


def test_proxy_allows_in_scope_and_logs_event(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )
    request: dict = {
        "target": f"{http_server}/data",
        "host_header": f"127.0.0.1:{port}",
        "token": TOKEN,
    }
    run_proxy(proxy, [request])
    assert request["status"] == 200
    assert request["body"] == b"egress-ok"
    assert len(proxy.events) == 1
    event = proxy.events[0]
    assert event.verdict == "allowed"
    assert event.host == "127.0.0.1"
    assert event.port == port
    assert event.bytes_received > 0


def test_proxy_requires_token(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )
    request: dict = {
        "target": f"{http_server}/data",
        "host_header": f"127.0.0.1:{port}",
        "token": None,
    }
    wrong: dict = {
        "target": f"{http_server}/data",
        "host_header": f"127.0.0.1:{port}",
        "token": "wrong-token",
    }
    run_proxy(proxy, [request, wrong])
    assert request["status"] == 407
    assert wrong["status"] == 407
    assert [event.verdict for event in proxy.events] == ["denied-auth", "denied-auth"]


def test_proxy_denies_out_of_scope_and_port(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",), excludes=("127.0.0.2",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )
    out_of_scope: dict = {
        "target": f"http://10.9.9.9:{port}/data",
        "host_header": f"10.9.9.9:{port}",
        "token": TOKEN,
    }
    run_proxy(proxy, [out_of_scope])
    assert out_of_scope["status"] == 403
    assert proxy.events[0].verdict == "denied-scope"

    proxy2 = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)),
        TOKEN,
        allowed_ports=frozenset({443}),
    )
    wrong_port: dict = {
        "target": f"{http_server}/data",
        "host_header": f"127.0.0.1:{port}",
        "token": TOKEN,
    }
    run_proxy(proxy2, [wrong_port])
    assert wrong_port["status"] == 403
    assert proxy2.events[0].verdict == "denied-port"


def test_proxy_connect_tunnel(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )

    async def exercise() -> tuple[int, bytes]:
        _, proxy_port = await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(
                f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Proxy-Authorization: Bearer {TOKEN}\r\n\r\n".encode()
            )
            await writer.drain()
            response_line = await reader.readline()
            assert b"200" in response_line
            await reader.readline()
            writer.write(b"GET /data HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            await writer.drain()
            payload = await reader.read(-1)
            writer.close()
            return 200, payload
        finally:
            await proxy.stop()

    status, payload = asyncio.run(exercise())
    assert status == 200
    assert b"egress-ok" in payload
    assert proxy.events[0].verdict == "allowed"
    assert proxy.events[0].bytes_received > 0


def test_proxy_stopped_after_use(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)), TOKEN, allowed_ports=frozenset({port})
    )

    async def exercise() -> tuple[str, int]:
        return await proxy.start()

    _, proxy_port = asyncio.run(exercise())
    asyncio.run(proxy.stop())
    with pytest.raises(OSError):
        asyncio.run(asyncio.open_connection("127.0.0.1", proxy_port))


class _ProxyUsingManager:
    def __init__(self, target_url: str) -> None:
        self.target_url = target_url

    async def run(
        self, argv: list[str] | tuple[str, ...], timeout_seconds: float | None = None
    ) -> ProcessResult:
        arguments = tuple(argv)
        if arguments[:2] != ("docker", "run"):
            return ProcessResult(exit_code=0, stdout="ok", stderr="")
        proxy_url = next(
            arguments[index + 1]
            for index, part in enumerate(arguments)
            if part == "--env" and arguments[index + 1].startswith("HTTP_PROXY=")
        )[len("HTTP_PROXY="):]
        return await asyncio.to_thread(self._fetch_via_proxy, proxy_url)

    def _fetch_via_proxy(self, proxy_url: str) -> ProcessResult:
        authority = proxy_url.split("@", 1)[1]
        token = proxy_url.split("//", 1)[1].split("@", 1)[0].split(":", 1)[1]
        proxy_port = int(authority.rsplit(":", 1)[1])
        connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
        connection.request(
            "GET", self.target_url, headers={"Proxy-Authorization": f"Bearer {token}"}
        )
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return ProcessResult(
            exit_code=0 if response.status == 200 else 1,
            stdout=body.decode("utf-8", "replace"),
            stderr="",
        )


def test_scoped_sandbox_run_logs_egress_events(
    tmp_path: Path, http_server: str
) -> None:
    server_port = int(http_server.rsplit(":", 1)[1])
    sign_allowlist(tmp_path / "sandbox-allowlist.sig", MASTER_KEY)
    database_path = tmp_path / "scoped.db"
    database = Database(database_path)
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    manager = _ProxyUsingManager(f"{http_server}/data")
    runner = DockerSandboxRunner(
        manager,  # type: ignore[arg-type]
        tmp_path / "sandbox-allowlist.sig",
        lambda: MASTER_KEY,
        egress_allowed_ports=frozenset({server_port}),
    )
    execution = ToolExecutionService(
        service,
        kernel,
        recovery,
        ToolRunRepository(database),
        registry=ToolRegistry(adapters=(SandboxExecTool(runner),)),
    )
    operation = service.create(
        "Scoped lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("127.0.0.1",)),
    )
    params = {
        "profile": "sandbox.scoped@v1",
        "executable": "wget",
        "args": ["-q", "-O-", f"{http_server}/data"],
    }
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, "sandbox.exec", params)
    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id, "sandbox.exec", params, approval_id=caught.value.approval_id
    )
    assert result.run.status == ToolRunStatus.COMPLETED
    assert result.stdout == "egress-ok"

    events = service.events(operation.id)
    completed = [
        event for event in events if event.event_type == "tool.run.completed"
    ]
    assert len(completed) == 1
    egress = completed[0].payload["egress"]
    assert egress[0]["host"] == "127.0.0.1"
    assert egress[0]["port"] == server_port
    assert egress[0]["verdict"] == "allowed"
    assert egress[0]["bytes_received"] > 0
    import json

    events_dump = json.dumps([event.payload for event in events])
    assert "samurai:" not in events_dump


def test_generate_token_unique() -> None:
    assert generate_proxy_token() != generate_proxy_token()
