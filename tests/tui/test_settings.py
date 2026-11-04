"""The settings screen: catalogue rows, masked input, state markers."""

import asyncio
from pathlib import Path

from test_app import _minimal_stack  # noqa: F401  (same path as the other TUI tests)

from samurai_ai.i18n import Translator
from samurai_ai.tui.app import SamuraiApp
from samurai_ai.tui.screens.settings import SettingsScreen


def test_the_catalogue_is_listed_with_state_markers() -> None:
    translations = Translator("ru")
    screen = SettingsScreen(translations, {"SHODAN_API_KEY": "set"}, lambda *_: None,
                            lambda *_: None)
    options = screen._options()
    labels = [str(option.prompt) for option in options]
    assert any("Shodan" in label for label in labels)
    assert any(label.startswith(translations.t("settings.set")) for label in labels)
    assert any(label.startswith(translations.t("settings.empty")) for label in labels)


def test_every_row_is_a_catalogue_entry() -> None:
    screen = SettingsScreen(Translator("en"), {}, lambda *_: None, lambda *_: None)
    assert all(option.id for option in screen._options())
    assert screen.rows and screen.rows[0].env_name.isupper()


def test_saving_a_key_marks_it_set_and_passes_it_on() -> None:
    saved: list[tuple[str, str]] = []
    screen = SettingsScreen(
        Translator("en"), {}, lambda env, value: saved.append((env, value)),
        lambda *_: None,
    )
    screen.states = {entry.env_name: "empty" for entry in screen.rows}

    async def exercise() -> None:
        app = SamuraiApp(*_minimal_stack(Path("/tmp/set1")))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(screen)
            await pilot.pause()
            screen.action_edit()
            await pilot.pause()
            field = screen.query_one("#settings-input")
            field.value = "secret-value"
            field.post_message(
                field.Submitted(field, "secret-value")
            )
            await pilot.pause()
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
    assert saved and saved[0][1] == "secret-value"


def test_clearing_a_key_marks_it_empty() -> None:
    cleared: list[str] = []
    screen = SettingsScreen(
        Translator("en"), {"SHODAN_API_KEY": "set"}, lambda *_: None, cleared.append
    )
    entry = screen.rows[0]

    async def exercise() -> None:
        app = SamuraiApp(*_minimal_stack(Path("/tmp/set2")))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.push_screen(screen)
            await pilot.pause()
            screen.action_clear()
            await pilot.pause()
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
    assert cleared == [entry.env_name]
    assert screen.states[entry.env_name] == "empty"
