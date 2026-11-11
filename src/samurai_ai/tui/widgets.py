"""Reusable widgets: selection-aware journal log, tabs, history input."""

from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.events import Mount
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Input, OptionList, RichLog, Static, Tabs


class SelectableRichLog(RichLog, can_focus=False):
    """RichLog that participates in mouse text selection (copy with Ctrl+C).

    RichLog strips carry no text-offset metadata, so the compositor cannot
    map mouse coordinates back to text. render_line retags every strip with
    `apply_offsets` (the same mechanism Content widgets use) and paints the
    active selection span. Not focusable, so clicking the journal to select
    text never steals focus from the command input.
    """

    @property
    def allow_select(self) -> bool:
        return True

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(strip.text for strip in self.lines)
        if not text:
            return None
        return selection.extract(text), "\n"

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        scroll_x, scroll_y = self.scroll_offset
        row = scroll_y + y
        if row >= len(self.lines):
            return strip
        strip = strip.apply_offsets(scroll_x, row)
        selection = self.text_selection
        if selection is not None:
            span = selection.get_span(row)
            if span is not None and span[0] < span[1]:
                style = self.screen.get_component_rich_style("screen--selection")
                start, end = span
                # post_style paints the selection *on top* of existing colors
                # (Strip.apply_style would put the segment's own background last).
                highlighted = Strip(
                    Segment.apply_style(list(strip[start:end]), post_style=style),
                    end - start,
                )
                strip = Strip.join(
                    [
                        strip[0:start],
                        highlighted,
                        strip[end : strip.cell_length],
                    ]
                )
        return strip


class StripItem(Static):
    """A status-strip cell that opens what it summarises on click."""

    def __init__(self, target: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.target = target

    def on_click(self) -> None:
        opener = getattr(self.app, "_strip_open", None)
        if opener is not None:
            opener(self.target)


class SideBlock(Static):
    """A side-panel list (SCOPE, LOOT); a click opens the row or the full list.

    Rows carry the domain id they came from, so a click reaches the evidence
    behind a loot entry instead of stopping at the row text.
    """

    #: how many rows the panel shows before the rest fold behind «ещё N…»
    LIMIT = 10

    def __init__(self, target: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.target = target
        #: every row as a plain line, for the full list behind the click
        self.entries: list[str] = []
        #: id per row position in the text, "" where a row opens nothing
        self.row_ids: list[str] = []

    def on_click(self, event) -> None:  # type: ignore[no-untyped-def]
        index = int(getattr(event, "y", 0)) - self._header_lines()
        visible = 0 <= index < min(len(self.row_ids), self.LIMIT)
        if visible and self.row_ids[index]:
            opener = getattr(self.app, "_open_loot_source", None)
            if opener is not None:
                opener(self.row_ids[index])
                return
        if len(self.entries) <= self.LIMIT:
            return
        # the «ещё N…» fold, and any row that proves nothing, open the whole list
        full = getattr(self.app, "_show_side_list", None)
        if full is not None:
            full(self.target, self.entries)

    def _header_lines(self) -> int:
        """Rows sit under the block title; the LOOT block has none in SCOPE."""
        return 1


class OperationTab(Static):
    """One open operation in the strip: click focuses it, right-click closes."""

    def __init__(
        self,
        operation_id: str,
        label: str,
        *,
        busy: bool = False,
        current: bool = False,
    ) -> None:
        super().__init__()
        self.operation_id = operation_id
        self.label = label
        self.busy = busy
        self.current = current
        self.update(self.render_label(label))

    def render_label(self, label: str) -> Text:
        mark = "●" if self.current else ("▶" if self.busy else "○")
        text = Text(f" {mark} {label} ")
        return text

    def on_click(self, event) -> None:  # type: ignore[no-untyped-def]
        if getattr(event, "button", 1) == 3:
            menu = getattr(self.app, "_tab_menu", None)
            if menu is not None:
                menu(self.operation_id, self.label)
            return
        opener = getattr(self.app, "_open_operation", None)
        if opener is not None:
            opener(self.operation_id)


class ScopeList(OptionList):
    """The scope tab's rule list.

    A left-click keeps the old behaviour (toggle the rule's exclusion through
    the list's own selection). A right-click opens an edit menu for the rule the
    cursor is on — exclude/restore, remove or rename — applied to the operation
    at once, so the operator can shape the scope by hand even while the agent
    widens it on its own.
    """

    def on_click(self, event: events.Click) -> None:
        if getattr(event, "button", 1) != 3:
            return  # left-click falls through to normal selection
        event.stop()
        index = self.highlighted
        if index is None:
            return
        option = self.get_option_at_index(index)
        rule = str(getattr(option, "id", "") or "")
        menu = getattr(self.app, "_scope_rule_menu", None)
        if rule and menu is not None:
            menu(rule)


class PlusButton(Static):
    """Clickable '+' in the topbar — opens the new-operation dialog."""

    def on_click(self) -> None:
        handler = getattr(self.app, "_show_new_operation", None)
        if handler is not None:
            handler()


class ViewTabs(Tabs):
    """Tabs that stay inactive until the operator picks one.

    Stock Tabs force-activates the first tab on mount, which would switch the
    main view away from the chat journal before the first render.
    """

    def _on_mount(self, event: Mount) -> None:
        if self._first_active:
            self.active = self._first_active


class HistoryInput(Input):
    """Command input with shell-like history and slash-command suggestions."""

    BINDINGS = [
        Binding("up", "history_previous", show=False),
        Binding("down", "history_next", show=False),
        Binding("tab", "accept_suggestion", show=False),
        Binding("escape", "close_suggestions", show=False),
    ]

    def on_key(self, event: events.Key) -> None:
        """`?` on an empty field opens the key reference instead of typing."""
        if getattr(event, "key", "") != "question_mark" or self.value:
            return
        event.stop()
        event.prevent_default()
        show_help = getattr(self.app, "action_show_help", None)
        if show_help is not None:
            show_help()

    def action_history_previous(self) -> None:
        if self._suggestions_step(-1):
            return
        step = getattr(self.app, "history_step", None)
        if step is not None:
            step(-1)

    def action_history_next(self) -> None:
        if self._suggestions_step(1):
            return
        step = getattr(self.app, "history_step", None)
        if step is not None:
            step(1)

    def action_accept_suggestion(self) -> None:
        accept = getattr(self.app, "accept_suggestion", None)
        if accept is not None:
            accept()

    def action_close_suggestions(self) -> None:
        close = getattr(self.app, "close_suggestions", None)
        if close is not None:
            close()

    def _suggestions_step(self, direction: int) -> bool:
        step = getattr(self.app, "suggestions_step", None)
        if step is None:
            return False
        return bool(step(direction))
