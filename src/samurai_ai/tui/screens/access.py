"""Allow/deny prompt for a governed action (a remote model call, a shell command).

The command is shown in full and in a scrollable pane from the start: deciding
on an ellipsis is how a wrong command gets approved. Everything the harness
knows about the consequences — risk class, working directory, execution path,
timeout, destructive/sensitive marks — sits above it, so the operator reads the
facts before the body.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from samurai_ai.i18n import Translator


@dataclass(frozen=True)
class ApprovalMeta:
    """What the harness knows about one governed action, for the prompt."""

    tool_id: str = ""
    #: catalogue version of the tool, when the tool comes from a recipe
    tool_version: int = 0
    cwd: str = ""
    #: native host, relay (with its command) or the pinned container
    execution_path: str = ""
    timeout_s: float = 0.0
    destructive: tuple[str, ...] = ()
    sensitive: tuple[str, ...] = ()
    #: other calls riding along in the same batch
    batch: tuple[str, ...] = field(default=())

    @property
    def badges(self) -> str:
        return ", ".join((*self.destructive, *self.sensitive))


class AccessPromptScreen(ModalScreen[bool]):
    """Allow/deny prompt for a governed action (e.g. a remote model call)."""

    CSS = """
    AccessPromptScreen {
        align: center middle;
    }
    #access-dialog {
        width: 92%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: solid $warning;
        background: $panel;
    }
    #access-title {
        text-style: bold;
        color: $warning;
        padding-bottom: 1;
    }
    #access-action {
        color: $text;
        padding-bottom: 1;
    }
    #access-meta {
        color: $text-dim;
        padding-bottom: 1;
    }
    #access-badges {
        color: $warning;
        text-style: bold;
        padding-bottom: 1;
    }
    #access-options {
        height: auto;
        background: $panel;
    }
    #access-full {
        height: auto;
        max-height: 16;
        border: solid $border;
        background: $surface;
        padding: 0 1;
    }
    #access-full-text {
        width: auto;
    }
    #access-hint {
        color: $text-dim;
        padding-top: 1;
    }
    """

    BINDINGS = [
        ("v", "toggle_full", "full command"),
        ("c", "copy_command", "copy"),
        ("a", "allow_batch", "allow all"),
    ]

    def __init__(
        self,
        translator: Translator,
        action: str,
        risk: str,
        *,
        meta: ApprovalMeta | None = None,
        batch_items: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.action = action
        self.risk = risk
        self.meta = meta or ApprovalMeta()
        self.batch_items = batch_items or []

    def compose(self) -> ComposeResult:
        meta = self.meta
        with Vertical(id="access-dialog"):
            yield Static(self.translator.t("access.prompt-title"), id="access-title")
            if self.batch_items:
                yield Static(
                    self.translator.t("access.batch-hint", count=len(self.batch_items)),
                    id="access-batch",
                )
            yield Static(self._subject_line(), id="access-action")
            badges = meta.badges
            if badges:
                yield Static(f"⚠ {badges}", id="access-badges")
            facts = self._facts()
            if facts:
                yield Static(facts, id="access-meta")
            yield OptionList(
                Option(self.translator.t("access.allow"), id="a-allow"),
                Option(self.translator.t("access.deny"), id="a-deny"),
                id="access-options",
            )
            with VerticalScroll(id="access-full"):
                yield Static(self._command_body(), id="access-full-text")
            yield Static(self.translator.t("access.full-hint"), id="access-hint")

    def _subject_line(self) -> str:
        """The whole line folded to one row: the body below is the full text."""
        flat = " ⏎ ".join(line for line in self.action.splitlines() if line.strip())
        return flat if len(flat) < 120 else f"{flat[:117]}…"

    def _facts(self) -> str:
        """One line of consequences; unknown values are simply left out."""
        meta = self.meta
        parts = [f"{meta.tool_id}" if meta.tool_id else "", f"risk={self.risk}"]
        if meta.tool_version:
            parts.append(f"v{meta.tool_version}")
        if meta.cwd:
            parts.append(f"cwd={meta.cwd}")
        if meta.execution_path:
            parts.append(self.translator.t("access.path", path=meta.execution_path))
        if meta.timeout_s:
            parts.append(self.translator.t("access.timeout", seconds=int(meta.timeout_s)))
        if self.batch_items:
            parts.append(self.translator.t("access.batch-count", count=len(self.batch_items)))
        return " · ".join(parts)

    def _command_body(self) -> str:
        """The command as it will run, with a numbered body when it is multiline."""
        lines = self.action.splitlines() or [""]
        if len(lines) <= 1:
            return self.action
        width = len(str(len(lines)))
        return "\n".join(f"{index:>{width}} │ {line}" for index, line in enumerate(lines, 1))

    def on_mount(self) -> None:
        options = self.query_one("#access-options", OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option is not None and event.option.id == "a-allow")

    def reveal(self, shown: bool) -> None:
        """Collapse or restore the body; the facts stay either way."""
        self.query_one("#access-full", VerticalScroll).display = shown
        self.query_one("#access-hint", Static).update(
            self.translator.t("access.full-hint-shown" if shown else "access.full-hint")
        )

    def action_toggle_full(self) -> None:
        self.reveal(not self.query_one("#access-full", VerticalScroll).display)

    def action_copy_command(self) -> None:
        self.app.copy_to_clipboard(self.action)
        self.query_one("#access-hint", Static).update(self.translator.t("access.copied"))

    def action_allow_batch(self) -> None:
        if self.batch_items:
            self.dismiss(True)

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(False)
        elif event.key == "enter":
            # Enter is handled here, not by OptionList: its highlight is not set
            # yet on the first frame, which made the very first Enter a no-op.
            options = self.query_one("#access-options", OptionList)
            self.dismiss(options.highlighted in (None, 0))
