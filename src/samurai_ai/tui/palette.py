"""Command palette provider built from the action registry.

The palette is a view of `tui.commands`, so every action the operator can reach
from a slash is reachable from `Ctrl-P` — and neither list can drift, because
both read the same table.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from textual.command import DiscoveryHit, Hit, Hits, Provider

from samurai_ai.tui.commands import COMMANDS, GROUPS, CommandSpec

if TYPE_CHECKING:
    from samurai_ai.tui.app import SamuraiApp


class CommandProvider(Provider):
    """Searches the action registry for the operator's query."""

    @property
    def app(self) -> SamuraiApp:  # type: ignore[override]
        return super().app  # type: ignore[return-value]

    def _describe(self, spec: CommandSpec) -> str:
        """Strip the command column from the help line: it repeats the name."""
        line = self.app.translator.t(spec.help_key)
        return re.split(r"\s{2,}", line, maxsplit=1)[-1]

    def _label(self, spec: CommandSpec) -> str:
        return spec.name

    def _help(self, spec: CommandSpec) -> str:
        group = self.app.translator.t(f"palette.group.{spec.group}")
        return f"{group} · {self._describe(spec)}"

    def _callback(self, spec: CommandSpec) -> Any:
        app = self.app

        def run() -> None:
            app.run_worker(app.invoke_command(spec), exclusive=False)

        return run

    async def discover(self) -> Hits:
        for group in GROUPS:
            for spec in COMMANDS:
                if spec.group != group:
                    continue
                yield DiscoveryHit(
                    display=self._label(spec),
                    command=self._callback(spec),
                    text=spec.name,
                    help=self._help(spec),
                )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        scored: list[tuple[float, CommandSpec]] = []
        for spec in COMMANDS:
            haystack = f"{spec.name} {self._describe(spec)}"
            score = matcher.match(haystack)
            if score > 0:
                scored.append((score, spec))
        for score, spec in sorted(scored, key=lambda pair: -pair[0]):
            yield Hit(
                score,
                matcher.highlight(self._label(spec)),
                command=self._callback(spec),
                text=spec.name,
                help=self._help(spec),
            )
