"""The mark: its geometry, its frames, and the CLI banner built from it."""

from __future__ import annotations

import pytest

from samurai_ai.tui import brand


def _plain(mark: brand.Rows | None = None) -> str:
    return brand.render(brand.Painter({}), mark).plain


def test_mark_lines_are_all_one_width() -> None:
    widths = {len(line) for line in _plain().split("\n")}
    assert widths == {brand.width()}


def test_mark_has_a_katana_and_an_arc() -> None:
    pieces = {piece for row in brand.MARK for piece, _ in row}
    assert pieces == {brand.ARC, brand.BLADE, brand.GUARD, brand.HILT, brand.PAD}
    assert "╪" in _plain()  # the guard
    assert "╭" in _plain()  # the arc leaves the blade


@pytest.mark.parametrize("step", range(8))
def test_every_full_frame_stays_square(step: int) -> None:
    frame = brand.full_frame(step, brand.Painter({})).plain
    assert {len(line) for line in frame.split("\n")} == {brand.width()}


def test_arc_breathes_out_and_back() -> None:
    lengths = [
        brand.full_frame(step, brand.Painter({})).plain.split("\n")[0].count("─")
        for step in range(4)
    ]
    assert lengths[0] < lengths[1] < lengths[2]
    assert lengths[3] < lengths[2]


@pytest.mark.parametrize("step", range(6))
def test_edge_frame_is_one_row(step: int) -> None:
    line = brand.frame(step, brand.Painter({})).plain
    assert "\n" not in line
    assert line.startswith("╰")
    assert line.endswith("╪")


def test_painter_styles_the_steel_and_the_arc_differently() -> None:
    painted = brand.render(brand.Painter({brand.BLADE: "#111111", brand.ARC: "#ff0000"}))
    styles = {span.style for span in painted.spans}
    assert "#111111" in styles
    assert "#ff0000" in styles
    assert "#111111" != "#ff0000"


def test_version_banner_prints_the_mark_and_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    from samurai_ai import __version__
    from samurai_ai.cli import print_version_banner

    print_version_banner()
    out = capsys.readouterr().out
    assert "╪" in out
    assert __version__ in out
    assert "samurai" in out


def test_version_flag_prints_the_banner(capsys: pytest.CaptureFixture[str]) -> None:
    from samurai_ai.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])
    out = capsys.readouterr().out
    assert "╪" in out
    assert "authorised operations" in out
