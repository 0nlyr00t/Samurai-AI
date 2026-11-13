"""Block-based journal: the chat transcript as a scroll of widgets.

RichLog carried the first waves as flat text. Drill-down needs real widgets:
tool runs as collapsible blocks with a status icon, entity links that answer
clicks, and copy-any-time selection. Modern Textual selects text across
widgets on its own, so the block journal needs no selection overlay — a plain
``Static`` is selectable out of the box.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Markdown, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

#: body lines shown when a tool block is opened; the middle is cut
_TOOL_BODY_LIMIT = 60


def _selection_text(visual: Any, selection: Selection) -> tuple[str, str] | None:
    """Extract a selection from rendered text, surviving an offset past the end.

    Textual records a drag on the widget under the pointer and asks that widget
    for its own text, so a pointer that lands on a block's trailing blank row
    points a line beyond it — and `Selection.extract` indexes the start line
    blindly. Clamping the start to the last line keeps the copy alive, and copies
    that line rather than nothing.
    """
    if isinstance(visual, (Text, Content)):
        text = str(visual)
    elif isinstance(visual, str):
        text = visual
    else:
        return None
    start = selection.start
    if start is not None:
        last = max(len(text.splitlines()) - 1, 0)
        if start.y > last:
            selection = Selection(start=Offset(0, last), end=selection.end)
    return selection.extract(text), "\n"


class SelectableBlock(Static):
    """A journal block: selectable text that survives a stale offset.

    A drag that starts on the blank row under a short block, or a layout shift
    that moves the transcript, points past what the block renders; see
    `_selection_text` for why that used to raise out of ``Ctrl+C``.
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return _selection_text(self._render(), selection)


def entity_span(label: str, kind: str, ref: str, *, base_style: str = "") -> Text:
    """A text span that answers a click with ``app.action_inspect``.

    The action travels in the span's style meta, so journal text stays free of
    markup parsing — tool output may carry any brackets it likes.
    """
    style = Style.parse(base_style) if base_style else Style()
    return Text(
        label,
        style=style + Style(underline=True, meta={"@click": f"inspect('{kind}','{ref}')"}),
    )


class UserBlock(SelectableBlock):
    """The operator's message: padded, on the panel background, selectable."""

    DEFAULT_CSS = """
    UserBlock {
        height: auto;
        padding: 0 2;
        background: $panel-alt;
        text-style: bold;
    }
    """


class NoticeBlock(SelectableBlock):
    """An important event — a finding, a captured secret, an error — set apart.

    A burst of runs can bury a one-line finding; a notice gets a thick accent
    rail and padding so it stays visible in the flow, is tracked by the journal
    so the operator can jump to the latest one, and still carries any entity
    link in its text.
    """

    DEFAULT_CSS = """
    NoticeBlock {
        height: auto;
        padding: 0 2;
        margin: 0 1;
        border-left: thick $accent-alt;
        background: $panel;
    }
    """


class StreamBlock(SelectableBlock):
    """Consecutive plain writes merged into one selectable block.

    Mounting a widget per line would bury the compositor under thousands of
    Static children during a long run; merging consecutive text keeps the
    widget count near the number of *events*, not lines.
    """

    def __init__(self) -> None:
        super().__init__("")
        self._parts: list[Text] = []

    def append(self, text: Text) -> None:
        self._parts.append(text)
        self.update(Text("\n").join(self._parts))

    def replace_lines(self, parts: list[Text]) -> None:
        """Swap the whole content in place — no unmount/remount race."""
        self._parts = list(parts)
        self.update(Text("\n").join(self._parts))


class ToolHeader(SelectableBlock, can_focus=True):
    """The one-line summary of a tool run; click or Enter toggles the body."""

    BINDINGS = [Binding("enter", "toggle_body", show=False)]

    def on_click(self, event: events.Click) -> None:
        # a click that landed on a link span belongs to the link's action
        style = getattr(event, "style", None)
        if style is not None and "@click" in getattr(style, "meta", {}):
            return
        block = self.parent
        if isinstance(block, ToolBlock):
            block.toggle()

    def action_toggle_body(self) -> None:
        block = self.parent
        if isinstance(block, ToolBlock):
            block.toggle()


class LiveAnswerBlock(StreamBlock):
    """The model's answer as it streams: text lands token by token."""

    def append_token(self, token: str) -> None:
        parent = getattr(self, "parent", None)
        if parent is not None and not parent.follow:
            # the operator is reading up — buffer, do not yank the view
            self._parts.append(Text(token))
            return
        if self._parts:
            current = self._parts[-1]
            self._parts[-1] = Text(str(current) + token)
        else:
            self._parts = [Text(token)]
        self.update(Text("").join(self._parts))

    def reset_tokens(self) -> None:
        """Throw the streamed text away — a retry re-sends it from the start."""
        self._parts = []
        self.update(Text(""))

    def flush_tokens(self) -> None:
        """Show whatever was buffered while follow was off."""
        self.update(Text("").join(self._parts))

    def plain_text(self) -> str:
        """The streamed text so far (for the final message and /copy)."""
        return "".join(str(part) for part in self._parts)


class ToolBlock(Vertical):
    """A tool run: header line with a status icon plus a collapsible body.

    A failed run speaks for itself — the header carries the last error line
    and the body opens on its own, so a red ✗ never reads as a hang.
    """

    DEFAULT_CSS = """
    ToolBlock {
        height: auto;
    }
    ToolBlock > ToolHeader {
        height: auto;
    }
    ToolBlock > ToolHeader:hover {
        text-style: underline;
    }
    """

    def __init__(
        self,
        title: Text,
        body: list[str],
        *,
        failed: bool = False,
        body_style: str = "",
        expanded: bool | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._failed = failed
        self._body_style = body_style
        self.expanded = False if expanded is None else expanded

    def compose(self) -> ComposeResult:
        yield ToolHeader(self._render_title())
        yield SelectableBlock(self._render_body(), classes="tool-body")

    def set_title(self, title: Text) -> None:
        """Replace the header line (the thinking counter moves)."""
        self._title = title
        if not self.is_mounted:
            return
        self.query_one(ToolHeader).update(self._render_title())

    def set_body(self, body: list[str], style: str = "") -> None:
        """Replace the body in place — the thinking block grows during a turn."""
        self._body = body
        self._body_style = style
        if self.is_mounted:
            self.query_one(".tool-body", Static).update(self._render_body())
            self._apply_state()
            return
        # called before the children exist (freshly mounted): apply on refresh
        self.call_after_refresh(self._apply_after_mount)

    def _apply_after_mount(self) -> None:
        if not self.is_mounted:
            return
        body = self.query_one(".tool-body", Static)
        body.update(self._render_body())
        self._apply_state()

    def on_mount(self) -> None:
        self._apply_state()

    @property
    def failed(self) -> bool:
        return self._failed

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._apply_state()

    def _apply_state(self) -> None:
        try:
            header = self.query_one(ToolHeader)
            body = self.query_one(".tool-body", Static)
        except Exception:  # not mounted yet
            return
        header.update(self._render_title())
        body.display = self.expanded

    def _render_title(self) -> Text:
        marker = "▾" if self.expanded else "▸"
        return Text.assemble(marker, " ", self._title)

    def _render_body(self) -> Text:
        lines = self._body
        if len(lines) > _TOOL_BODY_LIMIT:
            keep_head = _TOOL_BODY_LIMIT // 2
            keep_tail = _TOOL_BODY_LIMIT - keep_head
            lines = [
                *lines[:keep_head],
                f"… {len(self._body) - _TOOL_BODY_LIMIT} more lines …",
                *lines[-keep_tail:],
            ]
        text = Text("\n").join(
            Text(f"  {line}", style=self._body_style) for line in lines
        )
        return text


class InspectSummaryBlock(SelectableBlock, can_focus=True):
    """Inline drill-down summary; Enter opens the full-screen inspector.

    First click on an entity link lands here — a few lines in the flow of the
    transcript. Enter (or the underlined link) goes to the full detail screen.
    """

    BINDINGS = [Binding("enter", "open_full", show=False)]

    DEFAULT_CSS = """
    InspectSummaryBlock {
        height: auto;
        padding: 0 2;
        border-left: thick $accent;
        margin: 0 1;
    }
    """

    def __init__(self, text: Text, kind: str, ref: str) -> None:
        super().__init__(text)
        self.kind = kind
        self.ref = ref

    def action_open_full(self) -> None:
        open_full = getattr(self.app, "_inspect_full", None)
        if open_full is not None:
            open_full(self.kind, self.ref)

    def on_click(self, event: events.Click) -> None:
        # a click on the summary itself (not the link) still goes deeper
        style = getattr(event, "style", None)
        if style is not None and "@click" in getattr(style, "meta", {}):
            return
        self.action_open_full()


class BlockJournal(VerticalScroll, can_focus=False):
    """The chat transcript: a vertical scroll of message and tool blocks.

    Drop-in for the old RichLog: ``write`` accepts the same renderables
    (``Text`` merges into the current stream block, a rich ``Markdown`` becomes
    a real Markdown widget, anything else becomes its own block). Clicks on
    entity spans reach ``app.action_inspect`` through the style meta.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tool_blocks: list[ToolBlock] = []
        #: important events (findings, secrets, errors) set apart as NoticeBlocks,
        #: tracked so the operator can jump to the latest one
        self.notices: list[NoticeBlock] = []
        #: the stream block still being appended to. Read from `children[-1]`
        #: it was lost whenever a mount had not landed yet, and the lines
        #: written in that window started a second, orphaned block
        self._last_stream: StreamBlock | None = None

    # -- RichLog-compatible surface -------------------------------------

    def write(self, renderable: Any) -> None:
        if isinstance(renderable, Text):
            self._stream().append(renderable)
        elif isinstance(renderable, RichMarkdown):
            self._mount(Markdown(renderable.markup))
        else:
            self._mount(SelectableBlock(renderable))

    def clear(self) -> None:
        self.tool_blocks.clear()
        self.notices.clear()
        self._last_stream = None
        self.remove_children()

    @property
    def lines(self) -> list[Any]:
        """Strip-shaped rows of the whole transcript (RichLog test compat)."""
        from textual.strip import Strip

        return [Strip([Segment(line)]) for line in self.text().splitlines()]

    def text(self) -> str:
        """Everything the journal shows, as plain text (tests, copy-all)."""
        from textual.content import Content

        parts: list[str] = []
        for child in self.walk_children():
            if child is self:
                continue
            render = getattr(child, "_render", None)
            visual = render() if callable(render) else None
            if isinstance(visual, (Text, Content)):
                parts.append(str(visual))
        return "\n".join(parts)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """A container selects across its blocks, not its own (empty) render."""
        return _selection_text(self.text(), selection)

    # -- block API ------------------------------------------------------

    def add_tool_block(
        self,
        title: Text,
        body: list[str],
        *,
        failed: bool = False,
        body_style: str = "",
        expanded: bool | None = None,
    ) -> ToolBlock:
        block = ToolBlock(
            title, body, failed=failed, body_style=body_style, expanded=expanded
        )
        self.tool_blocks.append(block)
        self._mount(block)
        return block

    def add_block(self, widget: Static) -> None:
        self._mount(widget)

    def set_lines(self, lines: list[Text]) -> None:
        """Panel content: one persistent stream block, updated in place.

        ``clear()`` then ``write()`` races — the scheduled removal can eat the
        freshly mounted block — so panels keep a single stream block and only
        swap its text.
        """
        if len(self.children) == 1 and isinstance(self.children[0], StreamBlock):
            self.children[0].replace_lines(lines)
            return
        self.remove_children()
        block = StreamBlock()
        block.replace_lines(lines)
        self._mount(block)

    def search(self, term: str) -> list[int]:
        """Line numbers (0-based) where the term shows up, for / and n/N."""
        if not term:
            return []
        lines = self.text().splitlines()
        lowered = term.lower()
        return [index for index, line in enumerate(lines) if lowered in line.lower()]

    def expand_oldest(self) -> ToolBlock | None:
        """Open the oldest collapsed block; the operator walks down the reply."""
        for block in self.tool_blocks:
            if not block.expanded:
                block.toggle()
                return block
        return None

    def add_notice(self, renderable: Any) -> NoticeBlock:
        """An important event, set apart with an accent rail and tracked."""
        block = NoticeBlock(renderable)
        self.notices.append(block)
        self._mount(block)
        return block

    def last_notice(self) -> NoticeBlock | None:
        return self.notices[-1] if self.notices else None

    def scroll_to_notice(self) -> NoticeBlock | None:
        """Bring the most recent notice into view (turns follow off)."""
        block = self.last_notice()
        if block is None:
            return None
        self._follow = False
        self.call_after_refresh(lambda: self.scroll_to_widget(block, animate=False))
        return block

    # -- internals ------------------------------------------------------

    def _stream(self) -> StreamBlock:
        if self._last_stream is not None:
            return self._last_stream
        block = StreamBlock()
        self._last_stream = block
        self._mount(block)
        return block

    def _mount(self, widget: Any) -> None:
        if not isinstance(widget, StreamBlock):
            self._last_stream = None
        self.mount(widget)
        if self.follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    @property
    def follow(self) -> bool:
        """Whether the journal still follows the tail (autoscroll)."""
        return getattr(self, "_follow", True)

    def toggle_follow(self) -> bool:
        """Flip follow mode; returns the new state."""
        self._follow = not self.follow
        if self._follow:
            self.call_after_refresh(self.scroll_end, animate=False)
        return self._follow

    def follow_end(self) -> None:
        """Jump to the tail and turn follow back on."""
        self._follow = True
        self.call_after_refresh(self.scroll_end, animate=False)

    def watch_scroll_y(self, old: float, new: float) -> None:
        """Follow the tail again once scrolled to the bottom; stop while reading up.

        The operator kept having to push the view down because follow, once off,
        only came back through a command. Now reaching the bottom re-arms
        autoscroll and scrolling up to read turns it off — the usual tail-f feel.
        """
        super().watch_scroll_y(old, new)
        try:
            self._follow = new >= self.max_scroll_y - 1
        except Exception:  # noqa: BLE001 - a scroll update must never raise
            pass
