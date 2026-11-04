"""The surface over a real socket: token, screens, downloads, read-only."""

import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from samurai_ai.web.server import Surface

TOKEN = "secret-token"


class FakeData:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.file = tmp_path / "rep_1.pdf"
        self.file.write_bytes(b"%PDF-1.7 fake")

    def operation(self) -> dict[str, Any]:
        return {"id": "op_1", "name": "Web <op>", "mode": "bug-bounty",
                "status": "execution", "objective": "", "includes": ["*.example.com"],
                "excludes": []}

    def counts(self) -> dict[str, int]:
        return {"tasks": 1, "findings": 1, "reports": 1}

    def events(self, limit: int) -> list[dict[str, Any]]:
        return [{"seq": 1, "type": "operation.created", "payload": "{}"}][:limit]

    def tasks(self) -> list[dict[str, Any]]:
        return [{"id": "t1", "status": "pending", "title": "Go"}]

    def findings(self) -> list[dict[str, Any]]:
        return [{"title": "XSS", "severity": "high", "status": "validated", "impact": None}]

    def reports(self) -> list[dict[str, Any]]:
        return [{"id": "rep_1", "format": "pdf", "size_bytes": 12}]

    def report_file(self, report_id: str) -> tuple[str, bytes] | None:
        if report_id != "rep_1":
            return None
        return "application/pdf", self.file.read_bytes()


@pytest.fixture
def surface(tmp_path: Path) -> Surface:
    instance = Surface(FakeData(tmp_path), TOKEN)
    instance.start()
    yield instance
    instance.stop()


def get(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - localhost
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def test_the_token_is_required(surface: Surface) -> None:
    status, body, _ = get(f"http://127.0.0.1:{surface.server.server_port}/")
    assert status == 401
    assert b"token" in body or "токен".encode() in body


def test_the_summary_escapes_the_operation_name(surface: Surface) -> None:
    port = surface.server.server_port
    status, body, media = get(f"http://127.0.0.1:{port}/?t={TOKEN}")
    assert status == 200 and media.startswith("text/html")
    assert b"Web &lt;op&gt;" in body
    assert "только чтение".encode() in body


def test_every_screen_answers(surface: Surface) -> None:
    port = surface.server.server_port
    for path in ("journal/", "plan/", "findings/", "reports/"):
        status, body, _ = get(f"http://127.0.0.1:{port}/{path}?t={TOKEN}")
        assert status == 200, path
        assert b"<table>" in body, path


def test_the_journal_page_refreshes_itself(surface: Surface) -> None:
    port = surface.server.server_port
    _, body, _ = get(f"http://127.0.0.1:{port}/journal/?t={TOKEN}")
    assert b'http-equiv="refresh"' in body
    assert b"<script" not in body


def test_a_report_downloads_with_its_media_type(surface: Surface) -> None:
    port = surface.server.server_port
    status, body, media = get(f"http://127.0.0.1:{port}/reports/rep_1/file?t={TOKEN}")
    assert status == 200 and media == "application/pdf"
    assert body.startswith(b"%PDF")


def test_an_unknown_page_and_report_are_404(surface: Surface) -> None:
    port = surface.server.server_port
    assert get(f"http://127.0.0.1:{port}/nope?t={TOKEN}")[0] == 404
    assert get(f"http://127.0.0.1:{port}/reports/rep_9/file?t={TOKEN}")[0] == 404
