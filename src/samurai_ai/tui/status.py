"""Width-aware composition of the one-line TUI status bar."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text


@dataclass(frozen=True)
class StatusSegment:
    """One styled fact; a larger ``drop_priority`` disappears sooner."""

    text: Text
    drop_priority: int
    min_width: int = 4


def _joined(segments: list[StatusSegment]) -> Text:
    result = Text()
    for index, segment in enumerate(segments):
        if index:
            result.append(" | ")
        result.append_text(segment.text)
    return result


def render_status(segments: list[StatusSegment], width: int) -> Text:
    """Drop secondary facts, then shorten them, without cropping the tail blind.

    The right-hand run clock, tool and notice counter are the facts an operator
    needs most during a turn. A plain string slice used to remove exactly those
    facts first because they happened to be appended last.
    """
    if width <= 0:
        return Text()
    kept = list(segments)
    while len(kept) > 1 and _joined(kept).cell_len > width:
        index = max(range(len(kept)), key=lambda item: (kept[item].drop_priority, item))
        segment = kept[index]
        if segment.text.cell_len <= segment.min_width:
            kept.pop(index)
            continue
        overflow = _joined(kept).cell_len - width
        target = max(segment.min_width, segment.text.cell_len - overflow)
        shortened = segment.text.copy()
        shortened.truncate(target, overflow="ellipsis")
        kept[index] = StatusSegment(shortened, segment.drop_priority, segment.min_width)

    # Very narrow terminals may not fit even the important set. Shorten the
    # least important remaining segment first, preserving its style and minimum.
    while _joined(kept).cell_len > width:
        candidates = [
            index
            for index, segment in enumerate(kept)
            if segment.text.cell_len > segment.min_width
        ]
        if not candidates:
            break
        index = max(candidates, key=lambda item: (kept[item].drop_priority, item))
        segment = kept[index]
        target = max(segment.min_width, segment.text.cell_len - (_joined(kept).cell_len - width))
        shortened = segment.text.copy()
        shortened.truncate(target, overflow="ellipsis")
        kept[index] = StatusSegment(shortened, segment.drop_priority, segment.min_width)

    result = _joined(kept)
    if result.cell_len > width:
        result.truncate(width, overflow="ellipsis")
    return result
