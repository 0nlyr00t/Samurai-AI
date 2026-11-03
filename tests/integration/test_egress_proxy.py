import asyncio
import http.client
import ipaddress
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.domain.execution import ApprovalRequiredError, ToolRunStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.egress import (
    EgressProxy,
    generate_proxy_token,
    parse_request_target,
)
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
    request["headers"] = " ".join(response.headers.get_all("Proxy-Authenticate") or [])
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
    # Firefox only understands Basic and gives up without a challenge it can
    # satisfy, so both schemes are advertised
    headers = request["headers"]
    assert "Basic" in headers and "Bearer" in headers
    assert wrong["status"] == 407
    assert [event.verdict for event in proxy.events] == ["denied-auth", "denied-auth"]


def test_extra_hosts_allow_a_third_party_besides_scope(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    # the scope names a host that is not 127.0.0.1: only the profile's extra
    # hosts may open the way to the passive source
    proxy = EgressProxy(
        ScopeContract(includes=("example.com",)),
        TOKEN,
        allowed_ports=frozenset({port}),
        extra_hosts=frozenset({"127.0.0.1"}),
    )
    request: dict = {
        "target": f"{http_server}/data",
        "host_header": f"127.0.0.1:{port}",
        "token": TOKEN,
    }
    run_proxy(proxy, [request])
    assert request["status"] == 200
    assert [event.verdict for event in proxy.events] == ["allowed"]


def test_extra_hosts_still_reject_unlisted_hosts(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("example.com",)),
        TOKEN,
        allowed_ports=frozenset({port}),
        extra_hosts=frozenset({"crt.sh"}),
    )
    request: dict = {
        "target": f"{http_server}/data",
        "host_header": f"127.0.0.1:{port}",
        "token": TOKEN,
    }
    run_proxy(proxy, [request])
    assert request["status"] == 403
    assert [event.verdict for event in proxy.events] == ["denied-scope"]
    assert not proxy._in_extra_hosts("127.0.0.1")


def test_extra_hosts_wildcard_matches_apex_and_subdomain() -> None:
    proxy = EgressProxy(
        ScopeContract(includes=("example.com",)),
        TOKEN,
        extra_hosts=frozenset({"*.commoncrawl.org", "crt.sh"}),
    )
    assert proxy._in_extra_hosts("commoncrawl.org")
    assert proxy._in_extra_hosts("index.commoncrawl.org")
    assert proxy._in_extra_hosts("crt.sh")
    assert not proxy._in_extra_hosts("notcommoncrawl.org")
    assert not proxy._in_extra_hosts("crt.sh.evil.com")


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


async def _socks5_session(
    proxy_port: int,
    address: str,
    address_type: int,
    port: int,
    *,
    token: str = TOKEN,
    username: bytes = b"samurai",
    command: int = 1,
    payload: bytes | None = None,
    offer_auth: bool = True,
) -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    result: dict = {}
    try:
        methods = b"\x00\x02" if offer_auth else b"\x00"
        writer.write(bytes([5, len(methods)]) + methods)
        await writer.drain()
        result["method"] = await reader.readexactly(2)
        if result["method"][1] != 2:
            return result
        writer.write(bytes([1, len(username)]) + username)
        writer.write(bytes([len(token)]) + token.encode())
        await writer.drain()
        result["auth"] = await reader.readexactly(2)
        if result["auth"][1] != 0:
            return result
        if address_type == 3:
            encoded = bytes([len(address)]) + address.encode("ascii")
        else:
            encoded = ipaddress.ip_address(address).packed
        writer.write(
            bytes([5, command, 0, address_type]) + encoded + port.to_bytes(2, "big")
        )
        await writer.drain()
        result["reply"] = await reader.readexactly(10)
        if result["reply"][1] != 0 or payload is None:
            return result
        writer.write(payload)
        await writer.drain()
        # the body may arrive in a later segment than the headers
        chunks: list[bytes] = []
        while chunk := await reader.read(65536):
            chunks.append(chunk)
        result["body"] = b"".join(chunks)
        return result
    finally:
        writer.close()
        await writer.wait_closed()


def run_socks5(proxy: EgressProxy, **kwargs: object) -> dict:  # type: ignore[no-untyped-def]
    async def scenario() -> dict:
        _, port = await proxy.start()
        try:
            return await _socks5_session(port, **kwargs)  # type: ignore[arg-type]
        finally:
            await proxy.stop()

    return asyncio.run(scenario())


def test_socks5_tunnels_to_an_allowed_host(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )
    # domain address type, the way a tool hands a host over
    payload = (
        f"GET {http_server}/data HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    result = run_socks5(
        proxy, address="127.0.0.1", address_type=3, port=port, payload=payload
    )
    assert result["method"] == b"\x05\x02"
    assert result["auth"] == b"\x01\x00"
    assert result["reply"][:2] == b"\x05\x00"
    assert b"egress-ok" in result["body"]
    events = [event for event in proxy.events if event.verdict == "allowed"]
    assert len(events) == 1
    assert events[0].host == "127.0.0.1"
    assert events[0].port == port
    assert events[0].bytes_sent > 0


def test_socks5_rejects_a_wrong_token(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )
    result = run_socks5(
        proxy, address="127.0.0.1", address_type=3, port=port, token="wrong-token"
    )
    assert result["auth"] == b"\x01\x01"
    assert [event.verdict for event in proxy.events] == ["denied-auth"]


def test_socks5_requires_the_userpass_method(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )
    result = run_socks5(
        proxy, address="127.0.0.1", address_type=3, port=port, offer_auth=False
    )
    assert result["method"] == b"\x05\xff"
    assert [event.verdict for event in proxy.events] == ["denied-auth"]


def test_socks5_denies_out_of_scope_and_foreign_port(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("example.com",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )
    outside = run_socks5(proxy, address="127.0.0.1", address_type=3, port=port)
    assert outside["reply"][:2] == b"\x05\x02"
    wrong_port = run_socks5(
        proxy, address="127.0.0.1", address_type=1, port=port + 1
    )
    assert wrong_port["reply"][:2] == b"\x05\x02"
    assert [event.verdict for event in proxy.events] == [
        "denied-scope",
        "denied-port",
    ]


def test_socks5_accepts_extra_hosts(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("example.com",)),
        TOKEN,
        allowed_ports=frozenset({port}),
        extra_hosts=frozenset({"127.0.0.1"}),
    )
    result = run_socks5(proxy, address="127.0.0.1", address_type=1, port=port)
    assert result["reply"][:2] == b"\x05\x00"


def test_socks5_rejects_an_unsupported_command(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("127.0.0.1",)),
        TOKEN,
        allowed_ports=frozenset({port}),
    )
    # only CONNECT is implemented, BIND (2) and UDP ASSOCIATE (3) are not
    result = run_socks5(
        proxy, address="127.0.0.1", address_type=1, port=port, command=3
    )
    assert result["reply"][:2] == b"\x05\x07"
    assert [event.verdict for event in proxy.events] == ["denied-protocol"]


def test_socks5_accepts_ipv6_address_type(http_server: str) -> None:
    port = int(http_server.rsplit(":", 1)[1])
    proxy = EgressProxy(
        ScopeContract(includes=("::1",)),
        TOKEN,
        allowed_ports=frozenset({port}),
        resolver=lambda host: ("127.0.0.1",),
    )
    result = run_socks5(
        proxy, address="::1", address_type=4, port=port
    )
    assert result["reply"][:2] == b"\x05\x00"
    assert proxy.events[0].host == "::1"


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
    stall_seconds: float = 120.0

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

def test_socks5_ipv6_target_is_scope_checked_with_brackets() -> None:
    proxy = EgressProxy(
        ScopeContract(includes=("::1",)), TOKEN, allowed_ports=frozenset({443})
    )
    assert proxy._verdict("::1", 443, "https") == "allowed"
    assert proxy._verdict("::2", 443, "https") == "denied-scope"


def test_connect_target_accepts_a_bracketed_ipv6_literal() -> None:
    scheme, host, port, _ = parse_request_target("CONNECT", "[::1]:443")
    assert (scheme, host, port) == ("https", "::1", 443)
