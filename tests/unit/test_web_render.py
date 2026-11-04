"""Rendering is pure HTML over rows: escaping, escaping, escaping."""

import re

from samurai_ai.web import render

TOKEN = "tok-123"


def test_the_page_carries_a_nav_and_the_token() -> None:
    body = render.page(title="op", body="x", token=TOKEN, active="plan/")
    assert "Samurai AI" in body and "план" in body
    assert f"?t={TOKEN}" in body
    assert 'class="on"' in body


def test_the_journal_page_can_refresh_itself_without_js() -> None:
    page = render.page(title="op", body="x", token=TOKEN, active="journal/", refresh=3)
    assert '<meta http-equiv="refresh" content="3">' in page
    assert "<script" not in page


def test_an_injected_title_is_escaped() -> None:
    body = render.render_findings([{
        "title": "<img src=x onerror=alert(1)>",
        "severity": "high", "status": "validated", "impact": None,
    }])
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert body.count("<img") == 0


def test_an_injected_event_payload_is_escaped() -> None:
    body = render.render_events([{"seq": 1, "type": "x", "payload": "<script>bad()</script>"}])
    assert "&lt;script&gt;bad()&lt;/script&gt;" in body


def test_report_links_point_at_the_download_with_the_token() -> None:
    body = render.render_reports([{"id": "rep_1", "format": "pdf", "size_bytes": 10}], TOKEN)
    assert f"/reports/rep_1/file?t={TOKEN}" in body


def test_empty_sections_say_so_instead_of_rendering_a_table() -> None:
    for body in (render.render_events([]), render.render_plan([]),
                 render.render_findings([]), render.render_reports([], TOKEN)):
        assert "нет" in body and "<table>" not in body


def test_the_summary_shows_scope_and_counts() -> None:
    body = render.render_summary(
        {"name": "op", "mode": "bug-bounty", "status": "execution",
         "includes": ["*.example.com"], "excludes": [], "objective": "x"},
        {"tasks": 2, "findings": 1, "reports": 0},
    )
    assert "*.example.com" in body and ">2<" in body
    assert re.search(r"<th>задач</th><td>2</td>", body)
