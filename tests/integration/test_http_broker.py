import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.domain.execution import (
    ToolExecutionError,
    ToolScopeError,
    ToolValidationError,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.http import HttpEgressBroker


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ok":
            self._respond(200, b"hello")
        elif self.path == "/host":
            self._respond(200, (self.headers.get("Host") or "").encode())
        elif self.path == "/big":
            self._respond(200, b"x" * 100000)
        elif self.path == "/redirect-ok":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/redirect-out":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.2:9/")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.end_headers()
        else:
            self._respond(404, b"missing")

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self._respond(200, b"posted:" + body)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


def make_broker(*excluded: str) -> HttpEgressBroker:
    return HttpEgressBroker(ScopeContract(includes=("127.0.0.1",), excludes=tuple(excluded)))


def test_fetch_allowed_url(http_server: str) -> None:
    response = make_broker().fetch(f"{http_server}/ok")
    assert response.status == 200
    assert response.body == b"hello"
    assert not response.truncated
    assert response.redirects == ()


def test_fetch_head_is_allowed(http_server: str) -> None:
    response = make_broker().fetch(f"{http_server}/ok", method="HEAD")
    assert response.status == 200
    assert response.body == b""


def test_fetch_rejects_write_methods_beyond_post(http_server: str) -> None:
    for method in ("PUT", "DELETE", "PATCH"):
        with pytest.raises(ToolValidationError, match="GET/HEAD/POST"):
            make_broker().fetch(f"{http_server}/ok", method=method)


def test_fetch_post_requires_body(http_server: str) -> None:
    with pytest.raises(ToolValidationError, match="require a body"):
        make_broker().fetch(f"{http_server}/ok", method="POST")
    with pytest.raises(ToolValidationError, match="only allowed with POST"):
        make_broker().fetch(f"{http_server}/ok", method="GET", body=b"{}")


def test_fetch_post_sends_body(http_server: str) -> None:
    response = make_broker().fetch(
        f"{http_server}/ok", method="POST", body=b'{"jsonrpc":"2.0"}'
    )
    assert response.status == 200
    assert response.body == b'posted:{"jsonrpc":"2.0"}'


def test_fetch_rejects_credentials_in_url(http_server: str) -> None:
    with pytest.raises(ToolValidationError, match="credentials"):
        make_broker().fetch("http://user:pw@127.0.0.1:9/ok")


def test_fetch_denies_out_of_scope_url() -> None:
    with pytest.raises(ToolScopeError, match="out-of-scope"):
        make_broker().fetch("http://127.0.0.2:9/")


def test_fetch_follows_allowed_redirect(http_server: str) -> None:
    response = make_broker().fetch(f"{http_server}/redirect-ok")
    assert response.status == 200
    assert response.body == b"hello"
    assert response.redirects == (f"{http_server}/ok",)


def test_fetch_denies_out_of_scope_redirect(http_server: str) -> None:
    with pytest.raises(ToolScopeError):
        make_broker().fetch(f"{http_server}/redirect-out")


def test_fetch_denies_excluded_redirect(http_server: str) -> None:
    broker = HttpEgressBroker(
        ScopeContract(includes=("127.0.0.0/24",), excludes=("127.0.0.2",))
    )
    with pytest.raises(ToolScopeError, match="excluded"):
        broker.fetch(f"{http_server}/redirect-out")


def test_fetch_denies_dns_excluded_hostname(http_server: str) -> None:
    broker = HttpEgressBroker(
        ScopeContract(includes=("localhost",), excludes=("127.0.0.1", "::1"))
    )
    with pytest.raises(ToolScopeError, match="excluded"):
        broker.fetch(http_server.replace("127.0.0.1", "localhost") + "/ok")


def test_fetch_enforces_redirect_limit(http_server: str) -> None:
    with pytest.raises(ToolExecutionError, match="redirect limit"):
        make_broker().fetch(f"{http_server}/redirect-loop")


def test_fetch_enforces_byte_limit(http_server: str) -> None:
    response = make_broker().fetch(f"{http_server}/big", max_bytes=100)
    assert response.truncated
    assert len(response.body) <= 100


def test_fetch_pins_connection_to_validated_address(http_server: str) -> None:
    port = http_server.rsplit(":", 1)[1]
    calls: list[str] = []

    def resolver(hostname: str) -> list[str]:
        calls.append(hostname)
        return ["127.0.0.1"]

    broker = HttpEgressBroker(
        ScopeContract(includes=("pinned.test",)), resolver=resolver
    )
    response = broker.fetch(f"http://pinned.test:{port}/ok")
    assert response.status == 200
    assert response.body == b"hello"
    assert calls == ["pinned.test"]

    host = broker.fetch(f"http://pinned.test:{port}/host")
    assert host.body == f"pinned.test:{port}".encode()


def test_fetch_ignores_resolver_answers_after_validation(http_server: str) -> None:
    port = http_server.rsplit(":", 1)[1]
    calls: list[str] = []

    def mutating_resolver(hostname: str) -> list[str]:
        calls.append(hostname)
        if len(calls) > 1:
            return ["127.0.0.2"]
        return ["127.0.0.1"]

    broker = HttpEgressBroker(
        ScopeContract(includes=("pinned.test",)), resolver=mutating_resolver
    )
    response = broker.fetch(f"http://pinned.test:{port}/ok")
    assert response.status == 200
    assert response.body == b"hello"
    assert len(calls) == 1


def test_fetch_denies_excluded_resolved_address(http_server: str) -> None:
    def resolver(hostname: str) -> list[str]:
        return ["127.0.0.2"]

    broker = HttpEgressBroker(
        ScopeContract(includes=("pinned.test",), excludes=("127.0.0.2",)),
        resolver=resolver,
    )
    with pytest.raises(ToolScopeError, match="excluded"):
        broker.fetch("http://pinned.test/ok")


def _self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pinned.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("pinned.test")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_https_fetch_tls_verify_toggle(tmp_path: Path) -> None:
    import ssl as ssl_module

    cert_path, key_path = _self_signed_cert(tmp_path)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    context = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        def resolver(hostname: str) -> list[str]:
            return ["127.0.0.1"]

        scope = ScopeContract(includes=("pinned.test",))
        url = f"https://pinned.test:{port}/ok"

        strict = HttpEgressBroker(scope, resolver=resolver)
        with pytest.raises(ToolExecutionError, match="http request failed"):
            strict.fetch(url)

        relaxed = HttpEgressBroker(scope, resolver=resolver, verify_tls=False)
        response = relaxed.fetch(url)
        assert response.status == 200
        assert response.body == b"hello"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
