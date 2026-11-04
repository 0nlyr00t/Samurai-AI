"""A tiny read-only HTTP front end for one operation.

Bound to 127.0.0.1, guarded by a per-run token taken from the link. It never
writes: every screen is a render of what the kernel already holds, and the only
binary it serves is a report file that already exists on disk.
"""

from __future__ import annotations

import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from samurai_ai.web import render

REFRESH_SECONDS = 3


class WebData(Protocol):
    """What the surface needs; the CLI wires it to the services."""

    def operation(self) -> dict[str, Any]: ...

    def counts(self) -> dict[str, int]: ...

    def events(self, limit: int) -> list[dict[str, Any]]: ...

    def tasks(self) -> list[dict[str, Any]]: ...

    def findings(self) -> list[dict[str, Any]]: ...

    def reports(self) -> list[dict[str, Any]]: ...

    def report_file(self, report_id: str) -> tuple[str, bytes] | None: ...


class Surface:
    def __init__(self, data: WebData, token: str) -> None:
        self.data = data
        self.token = token
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- routing ----------------------------------------------------------
    def dispatch(self, path: str, query: dict[str, list[str]]) -> tuple[int, str, bytes]:
        allowed = query.get("t", [""])[0]
        if not hmac.compare_digest(allowed, self.token):
            return 401, "text/html; charset=utf-8", _bytes(
                render.page(
                    title="нужен токен",
                    body=render.render_denied(),
                    token="",
                    active="",
                )
            )
        try:
            operation = self.data.operation()
        except Exception as exc:  # noqa: BLE001 - the page must still answer
            return 500, "text/html; charset=utf-8", _bytes(
                render.page(title="ошибка", body=render.render_error(str(exc)),
                            token=self.token, active="")
            )

        title = f'{operation.get("name", "операция")} — {operation.get("mode", "")}'
        if path in ("/", "/index.html"):
            body = render.render_summary(operation, self.data.counts())
            return 200, "text/html; charset=utf-8", _bytes(
                render.page(title=title, body=body, token=self.token, active="")
            )
        if path.startswith("/journal"):
            body = render.render_events(self.data.events(200))
            return 200, "text/html; charset=utf-8", _bytes(
                render.page(title=f"{title} · журнал", body=body, token=self.token,
                            active="journal/", refresh=REFRESH_SECONDS)
            )
        if path.startswith("/plan"):
            body = render.render_plan(self.data.tasks())
            return 200, "text/html; charset=utf-8", _bytes(
                render.page(title=f"{title} · план", body=body, token=self.token,
                            active="plan/")
            )
        if path.startswith("/findings"):
            body = render.render_findings(self.data.findings())
            return 200, "text/html; charset=utf-8", _bytes(
                render.page(title=f"{title} · находки", body=body, token=self.token,
                            active="findings/")
            )
        if path.startswith("/reports/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[2] == "file":
                served = self.data.report_file(parts[1])
                if served is None:
                    return 404, "text/plain; charset=utf-8", b"no such report"
                media, payload = served
                return 200, media, payload
            body = render.render_reports(self.data.reports(), self.token)
            return 200, "text/html; charset=utf-8", _bytes(
                render.page(title=f"{title} · отчёты", body=body, token=self.token,
                            active="reports/")
            )
        return 404, "text/html; charset=utf-8", _bytes(
            render.page(title="не найдено", body=render.render_error("нет такой страницы"),
                        token=self.token, active="")
        )

    # -- lifecycle --------------------------------------------------------
    def start(self, port: int = 0) -> str:
        surface = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                parsed = urlparse(self.path)
                status, media, payload = surface.dispatch(
                    parsed.path, parse_qs(parsed.query)
                )
                self.send_response(status)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{server.server_port}/?t={self.token}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def _bytes(text: str) -> bytes:
    return text.encode("utf-8")


__all__ = ["REFRESH_SECONDS", "Surface", "WebData"]
