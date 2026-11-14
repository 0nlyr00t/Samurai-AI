from rich.text import Text

from samurai_ai.tui.status import StatusSegment, render_status


def segment(text: str, priority: int, minimum: int = 4) -> StatusSegment:
    return StatusSegment(Text(text), priority, minimum)


def test_status_drops_secondary_facts_before_the_run_state() -> None:
    segments = [
        segment("~/very/long/workspace", 100, 8),
        segment("● ask", 20),
        segment("model=deepseek/deepseek-chat", 70, 8),
        segment("ctx 27%", 10),
        segment("⇈10k ⇊2k Σ12000", 80),
        segment("deepseek $1.2345", 90),
        segment("⏱ 02:14", 0),
        segment("▶ scan.nmap", 0),
        segment("⚑ 3", 0),
        segment("/help", 50),
    ]

    rendered = render_status(segments, 58)

    assert rendered.cell_len <= 58
    assert "⏱ 02:14" in str(rendered)
    assert "▶ scan.nmap" in str(rendered)
    assert "⚑ 3" in str(rendered)
    assert "ctx 27%" in str(rendered)
    assert "$1.2345" not in str(rendered)
    assert "deepseek-chat" not in str(rendered)
    assert "⇈10k" not in str(rendered)


def test_status_keeps_help_relevant_when_no_turn_is_running() -> None:
    rendered = render_status(
        [
            segment("~/workspace", 100),
            segment("● ask", 20),
            segment("ctx 0", 10),
            segment("/help", 15),
        ],
        30,
    )

    assert rendered.cell_len <= 30
    assert "● ask" in str(rendered)
    assert "ctx 0" in str(rendered)
    assert "/help" in str(rendered)


def test_status_shortens_least_important_remaining_segment_first() -> None:
    rendered = render_status(
        [segment("~/a-very-long-directory", 100, 8), segment("⏱ 02:14", 0), segment("⚑ 3", 0)],
        24,
    )

    assert rendered.cell_len <= 24
    assert "⏱ 02:14" in str(rendered)
    assert "⚑ 3" in str(rendered)
    assert str(rendered).startswith("~/a-")


def test_status_never_exceeds_even_a_tiny_width() -> None:
    rendered = render_status([segment("⏱ 02:14", 0), segment("⚑ 300", 0)], 7)

    assert rendered.cell_len <= 7
