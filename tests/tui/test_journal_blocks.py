from rich.text import Text

from samurai_ai.tui.journal import ToolBlock, elide_middle, format_duration


def test_middle_elision_keeps_tool_flags_and_final_target() -> None:
    command = "⚙ nmap · -sT -Pn --top-ports 1000 -oA evidence/scan 192.0.2.42"

    rendered = elide_middle(command, 48)

    assert Text(rendered).cell_len == 48
    assert rendered.startswith("⚙ nmap · -sT -Pn")
    assert rendered.endswith("192.0.2.42")
    assert "…" in rendered


def test_middle_elision_handles_short_and_wide_strings() -> None:
    assert elide_middle("nmap localhost", 40) == "nmap localhost"
    assert elide_middle("abcdef", 1) == "…"
    assert elide_middle("abcdef", 0) == ""
    assert Text(elide_middle("abcdef", 4)).cell_len == 4


def test_duration_is_compact_and_never_invented() -> None:
    assert format_duration(None) == ""
    assert format_duration(2.34) == "2.3s"
    assert format_duration(12.2) == "12s"
    assert format_duration(65) == "1:05"


def test_tool_output_pages_cover_lines_without_crossing_bounds() -> None:
    block = ToolBlock(Text("run"), [f"line-{index}" for index in range(130)])

    assert block.page_count == 3
    first = str(block._render_body())
    assert "line-0" in first and "line-59" in first
    assert "1/3" in first and "line-60" not in first

    block.next_page()
    second = str(block._render_body())
    assert block.expanded and block.page == 1
    assert "line-60" in second and "line-119" in second
    assert "2/3" in second

    block.next_page()
    block.next_page()  # the last page is a no-op, not an overflow
    third = str(block._render_body())
    assert block.page == 2
    assert "line-120" in third and "line-129" in third
    assert "3/3" in third

    block.previous_page()
    assert block.page == 1


def test_tool_output_is_bounded_with_a_visible_omission_marker() -> None:
    block = ToolBlock(Text("run"), [f"line-{index}" for index in range(500)])

    lines = block._display_lines()
    assert len(lines) == 240
    assert lines[0] == "line-0"
    assert "260 more lines" in lines[120]
    assert lines[-1] == "line-499"


def test_failed_tool_output_opens_on_its_own() -> None:
    assert ToolBlock(Text("failed"), ["boom"], failed=True).expanded
    assert not ToolBlock(Text("ok"), ["done"], failed=False).expanded
