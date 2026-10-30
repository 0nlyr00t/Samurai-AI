"""The product mark: a katana and the arc of its swing.

Shown large in the welcome pane and ``--version``, cut down to one row in the
busy indicator. Glyph and arc frames live here so the widget code and the tests
read the same data. Colours are tokens, resolved by the caller — the mark has to
work in the light theme too.
"""

from __future__ import annotations

from rich.text import Text

#: Pieces of the mark, in the order they are drawn. ``.`` pads, so every line is
#: the same width and the mark keeps its shape in any container.
BLADE = "blade"
GUARD = "guard"
HILT = "hilt"
ARC = "arc"
PAD = "pad"

#: A row is a run of (piece, cells); the mark is a run of rows.
Row = tuple[tuple[str, str], ...]
Rows = tuple[Row, ...]

#: (piece, cells) rows of the full mark. Every row is 16 cells wide so the mark
#: keeps its shape in any container.
MARK: Rows = (
    ((ARC, "  ╭────────────"), (PAD, " ")),
    ((PAD, "            "), (BLADE, "▄▄▄▄")),
    ((PAD, "        "), (BLADE, "▄▄▄▀▀▀"), (PAD, "  ")),
    ((PAD, "    "), (BLADE, "▄▄▀▀▀"), (PAD, "   "), (GUARD, "╪═╗"), (PAD, " ")),
    ((PAD, " "), (HILT, "▓▓▓▓▓▓▓"), (PAD, "        ")),
    ((PAD, "  "), (HILT, "▓▓▓"), (PAD, "           ")),
)

#: How much of the arc is drawn per frame: it breathes out, then back in.
ARC_LENGTHS: tuple[int, ...] = (0, 4, 12, 6)

_ARC_ROW = 0
_ARC_CELLS = 12
_ARC_INDENT = "  "

def _edge(arc: str) -> Rows:
    row: Row = ((ARC, arc), (BLADE, "▄▀"), (GUARD, "╪"))
    return (row,)


#: One-row edge for the busy indicator: the arc there is what breathes.
EDGE_FRAMES: tuple[Rows, ...] = (_edge("╰"), _edge("╰─"), _edge("╰──"), _edge("╰─"))


def _paint(piece: str, cells: str, paint: Painter) -> tuple[str, str]:
    if piece == PAD:
        return cells, ""
    return cells, paint(piece)


class Painter:
    """Maps a mark piece to a Rich style, so the caller owns the colours."""

    __slots__ = ("_styles",)

    def __init__(self, styles: dict[str, str]) -> None:
        self._styles = styles

    def __call__(self, piece: str) -> str:
        return self._styles.get(piece, "")


def render(paint: Painter, mark: Rows | None = None) -> Text:
    """Render the mark (or a frame of it) as one ``Text``."""
    rows: Rows = MARK if mark is None else mark
    block = Text()
    for index, row in enumerate(rows):
        if index:
            block.append("\n")
        for piece, cells in row:
            cells, style = _paint(piece, cells, paint)
            block.append(cells, style=style)
    return block


def frame(step: int, paint: Painter) -> Text:
    """One frame of the busy indicator, cycling through the arc lengths."""
    return render(paint, EDGE_FRAMES[step % len(EDGE_FRAMES)])


def full_frame(step: int, paint: Painter) -> Text:
    """A full mark with the arc at one of its breathing lengths."""
    length = ARC_LENGTHS[step % len(ARC_LENGTHS)]
    arc = " " * _ARC_CELLS
    if length:
        arc = "╭" + "─" * length
    rows: list[Row] = []
    for row_index, row in enumerate(MARK):
        carved: list[tuple[str, str]] = []
        for piece, cells in row:
            if piece == ARC and row_index == _ARC_ROW:
                carved.append((ARC, (_ARC_INDENT + arc).ljust(_ARC_CELLS + 3)))
                continue
            carved.append((piece, cells))
        rows.append(tuple(carved))
    return render(paint, tuple(rows))


def width() -> int:
    return max(sum(len(cells) for _, cells in row) for row in MARK)
