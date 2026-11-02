"""The CLI gate: an unconfigured install is a local admin, a configured one is not."""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from samurai_ai.cli import main

TEST_KEY = "cd" * 32
CLAIMS = {"sub": "u-1", "email": "v@test", "groups": ["readers"]}


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "cli-kid"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    """A properly signed token: the service verifies it against the JWKS below."""
    header = {"alg": "RS256", "kid": KID}
    pairs = {
        "iss": _IdpHandler.issuer,
        "aud": "guest",
        "exp": int(time.time()) + 3600,
        **claims,
    }
    signed = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(pairs).encode())}"
    signature = KEY.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signed}.{_b64(signature)}"


def _jwks() -> dict:
    numbers = KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "n": _b64(numbers.n.to_bytes(256, "big")),
                "e": _b64(numbers.e.to_bytes(3, "big")),
            }
        ]
    }


class _IdpHandler(BaseHTTPRequestHandler):
    issued: list[dict] = []
    issuer = ""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/jwks.json":
            return self._reply(200, _jwks())
        return self._reply(404, {})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/device_authorization":
            return self._reply(
                200,
                {"device_code": "dev", "user_code": "ABCD", "verification_uri": "https://idp/x"},
            )
        return self._reply(
            200, {"access_token": "at", "id_token": _jwt(CLAIMS), "expires_in": 600}
        )

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def idp() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IdpHandler)
    _IdpHandler.issuer = f"http://localhost:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _IdpHandler.issuer
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    return tmp_path


def test_an_unconfigured_cli_is_a_local_admin(home: Path, capsys) -> None:
    database = str(home / "a.db")

    assert main(["--db", database, "auth", "whoami"]) == 0
    assert "local (admin)" in capsys.readouterr().out
    assert main(["--db", database, "operation", "list"]) == 0


def test_a_configured_cli_refuses_work_before_login(
    home: Path, capsys, idp: str
) -> None:
    database = str(home / "b.db")
    assert main(["--db", database, "auth", "configure", idp, "guest"]) == 0
    capsys.readouterr()

    assert main(["--db", database, "operation", "list"]) == 2
    assert "not signed in" in capsys.readouterr().err


def test_login_then_a_viewer_reads_but_cannot_write(
    home: Path, capsys, idp: str
) -> None:
    database = str(home / "c.db")
    assert main(["--db", database, "auth", "configure", idp, "guest"]) == 0
    assert main(["--db", database, "role", "map", "readers", "viewer"]) == 0
    assert main(["--db", database, "auth", "login"]) == 0
    assert "signed in as v@test (viewer)" in capsys.readouterr().out

    assert main(["--db", database, "operation", "list"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "operation", "create", "Nope", "--mode", "ctf-htb",
                 "--include", "a.test"]) == 2
    assert "needs the admin role" in capsys.readouterr().err


def test_logout_closes_the_door_again(home: Path, capsys, idp: str) -> None:
    database = str(home / "d.db")
    assert main(["--db", database, "auth", "configure", idp, "guest"]) == 0
    assert main(["--db", database, "auth", "login"]) == 0
    capsys.readouterr()

    assert main(["--db", database, "auth", "logout"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "operation", "list"]) == 2


def test_an_unmapped_claim_gets_the_viewer_role(home: Path, capsys, idp: str) -> None:
    database = str(home / "e.db")
    assert main(["--db", database, "auth", "configure", idp, "guest"]) == 0
    assert main(["--db", database, "auth", "login"]) == 0

    assert main(["--db", database, "auth", "whoami"]) == 0
    assert "(viewer)" in capsys.readouterr().out
