"""HTML for the read-only web surface.

Pure functions over plain rows: no services, no request objects. Everything that
comes from an operation is escaped, and the pages carry no JavaScript — the
journal refreshes with a meta tag, like the rest of the project's plain output.
"""

from __future__ import annotations

import html
from typing import Any

_CSS = """
:root { color-scheme: dark }
body { background: #111; color: #e6e6e6; font: 14px/1.5 ui-monospace, Menlo, monospace;
       margin: 0; padding: 0 1rem 3rem }
header { display: flex; gap: 1rem; align-items: baseline; padding: .8rem 0;
         border-bottom: 1px solid #333; flex-wrap: wrap }
header a { color: #9cd; text-decoration: none }
header a.on { color: #fff; border-bottom: 2px solid #c33 }
h1 { font-size: 1.1rem; margin: 1rem 0 .5rem }
h2 { font-size: 1rem; margin: 1.2rem 0 .4rem; color: #c99 }
table { border-collapse: collapse; width: 100%; margin: .4rem 0 1rem }
th, td { text-align: left; border-bottom: 1px solid #262626; padding: .25rem .4rem;
         vertical-align: top; word-break: break-word }
th { color: #888; font-weight: 400 }
.muted { color: #777 }
.bad { color: #e66 }
.good { color: #6c6 }
.ev { white-space: pre-wrap }
.note { background: #1a1a1a; border-left: 3px solid #c33; padding: .5rem .8rem }
"""

_NAV: tuple[tuple[str, str], ...] = (
    ("", "сводка"),
    ("journal/", "журнал"),
    ("plan/", "план"),
    ("findings/", "находки"),
    ("reports/", "отчёты"),
)


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def page(*, title: str, body: str, token: str, active: str, refresh: int = 0) -> str:
    nav = "".join(
        _link(f"{href}", label, token, active == href) for href, label in _NAV
    )
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"{meta}<title>Samurai AI — {_escape(title)}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<header><strong>Samurai AI</strong>{nav}"
        f'<span class="muted">только чтение</span></header>'
        f"<h1>{_escape(title)}</h1>{body}</body></html>"
    )


def _link(href: str, label: str, token: str, on: bool) -> str:
    path = f"/{href}" if not href.endswith("/") else f"/{href}"
    if href == "":
        path = "/"
    css = ' class="on"' if on else ""
    return f'<a{css} href="{path}?t={_escape(token)}">{_escape(label)}</a>'


def render_summary(operation: dict[str, Any], counts: dict[str, int]) -> str:
    scope = ", ".join(operation.get("includes") or []) or "(пусто)"
    excludes = ", ".join(operation.get("excludes") or [])
    rows = [
        ("режим", operation.get("mode")),
        ("статус", operation.get("status")),
        ("scope", scope),
        ("исключения", excludes or "(нет)"),
        ("цель", operation.get("objective") or "(не задана)"),
        ("задач", counts.get("tasks", 0)),
        ("находок", counts.get("findings", 0)),
        ("отчётов", counts.get("reports", 0)),
    ]
    body = "<table>" + "".join(
        f"<tr><th>{_escape(name)}</th><td>{_escape(value)}</td></tr>" for name, value in rows
    ) + "</table>"
    return body


def render_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<p class="muted">событий пока нет</p>'
    lines = "".join(
        f'<tr><td class="muted">{_escape(event.get("seq"))}</td>'
        f'<td>{_escape(event.get("type"))}</td>'
        f'<td class="ev">{_escape(event.get("payload"))}</td></tr>'
        for event in events
    )
    return f"<table><tr><th>#</th><th>событие</th><th>данные</th></tr>{lines}</table>"


def render_plan(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return '<p class="muted">плана нет</p>'
    lines = "".join(
        f'<tr><td>{_escape(task.get("id"))}</td><td>{_escape(task.get("status"))}</td>'
        f'<td>{_escape(task.get("title"))}</td></tr>'
        for task in tasks
    )
    return (
        "<table><tr><th>задача</th><th>статус</th><th>название</th></tr>"
        f"{lines}</table>"
    )


def render_findings(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return '<p class="muted">находок нет</p>'
    lines = ""
    for finding in findings:
        impact = finding.get("impact") or "—"
        lines += (
            f'<tr><td>{_escape(finding.get("title"))}</td>'
            f'<td>{_escape(finding.get("severity"))}</td>'
            f'<td>{_escape(finding.get("status"))}</td>'
            f'<td>{_escape(impact)}</td></tr>'
        )
    return (
        "<table><tr><th>находка</th><th>серьёзность</th><th>статус</th>"
        f"<th>импакт</th></tr>{lines}</table>"
    )


def render_reports(reports: list[dict[str, Any]], token: str) -> str:
    if not reports:
        return '<p class="muted">отчётов нет</p>'
    lines = "".join(
        f'<tr><td>{_escape(report.get("id"))}</td>'
        f'<td>{_escape(report.get("format"))}</td>'
        f'<td>{_escape(report.get("size_bytes"))}</td>'
        f'<td><a href="/reports/{_escape(report.get("id"))}/file'
        f'?t={_escape(token)}">скачать</a></td></tr>'
        for report in reports
    )
    return (
        "<table><tr><th>id</th><th>формат</th><th>байт</th><th>файл</th></tr>"
        f"{lines}</table>"
    )


def render_denied() -> str:
    return (
        '<div class="note">Нужен токен из ссылки, которую напечатал '
        "<code>samurai web</code>: <code>?t=…</code></div>"
    )


def render_error(message: str) -> str:
    return f'<div class="note">{_escape(message)}</div>'


__all__ = [
    "page",
    "render_denied",
    "render_error",
    "render_events",
    "render_findings",
    "render_plan",
    "render_reports",
    "render_summary",
]
