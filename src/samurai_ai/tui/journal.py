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
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

#: body lines shown when a tool block is opened; the middle is cut
_TOOL_BODY_LIMIT = 60


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


class UserBlock(Static):
    """The operator's message: padded, on the panel background, selectable."""

    DEFAULT_CSS = """
    UserBlock {
        height: auto;
        padding: 0 2;
        background: $panel-alt;
        text-style: bold;
    }
    """


class StreamBlock(Static):
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


class ToolHeader(Static, can_focus=True):
    """The one-line summary of a tool run; click or Enter toggles the body."""

    BINDINGS = [Binding("enter", "toggle_body", show=False)]

    def on_click(self) -> None:
        block = self.parent
        if isinstance(block, ToolBlock):
            block.toggle()

    def action_toggle_body(self) -> None:
        block = self.parent
        if isinstance(block, ToolBlock):
            block.toggle()


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
        self.expanded = failed if expanded is None else expanded

    def compose(self) -> ComposeResult:
        yield ToolHeader(self._render_title())
        yield Static(self._render_body(), classes="tool-body")

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

    # -- RichLog-compatible surface -------------------------------------

    def write(self, renderable: Any) -> None:
        if isinstance(renderable, Text):
            self._stream().append(renderable)
        elif isinstance(renderable, RichMarkdown):
            self._mount(Markdown(renderable.markup))
        else:
            self._mount(Static(renderable))

    def clear(self) -> None:
        self.tool_blocks.clear()
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

    def get_selection(self, selection: Any) -> tuple[str, str] | None:
        """A container selects across its blocks, not its own (empty) render."""
        text = self.text()
        if not text:
            return None
        return selection.extract(text), "\n"

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

    def expand_oldest(self) -> ToolBlock | None:
        """Open the oldest collapsed block; the operator walks down the reply."""
        for block in self.tool_blocks:
            if not block.expanded:
                block.toggle()
                return block
        return None

    # -- internals ------------------------------------------------------

    def _stream(self) -> StreamBlock:
        tail = self.children[-1] if self.children else None
        if isinstance(tail, StreamBlock):
            return tail
        block = StreamBlock()
        self._mount(block)
        return block

    def _mount(self, widget: Any) -> None:
        self.mount(widget)
        self.call_after_refresh(self.scroll_end, animate=False)
