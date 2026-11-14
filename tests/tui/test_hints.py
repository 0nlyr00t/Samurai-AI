from rich.text import Text
from textual.screen import Screen

from samurai_ai.i18n import Translator
from samurai_ai.tui import keys
from samurai_ai.tui.hints import context_for, render_hints
from samurai_ai.tui.screens.access import AccessPromptScreen
from samurai_ai.tui.screens.help import HelpScreen


def test_idle_hints_use_live_keys_and_real_translation_keys() -> None:
    translator = Translator("en")
    keymap = keys.default_map()
    keymap["command_palette"] = "ctrl+x"

    rendered = render_hints("idle", keymap, translator.t, width=120)

    assert "Ctrl+X command palette" in rendered
    assert "Ctrl+R Resend the last message" in rendered
    assert "F1 key reference" in rendered
    assert "keys.action." not in rendered


def test_running_hints_only_include_background_for_a_live_shell() -> None:
    translator = Translator("en")
    keymap = keys.default_map()

    running = render_hints("running", keymap, translator.t, width=120)
    shell = render_hints("running", keymap, translator.t, width=120, shell_running=True)

    assert "Ctrl+C stop the run / quit" in running
    assert "Ctrl+S steer the running turn" in running
    assert "Ctrl+B" not in running
    assert "Ctrl+B background the shell job" in shell


def test_narrow_hints_keep_complete_first_actions_within_width() -> None:
    rendered = render_hints("idle", keys.default_map(), Translator("en").t, width=28)

    assert Text(rendered).cell_len <= 28
    assert rendered.startswith("Ctrl+P command palette")
    assert "keys.action." not in rendered


def test_modal_context_and_approval_specific_keys() -> None:
    translator = Translator("ru")
    approval = AccessPromptScreen(translator, "model.call", "kimi/kimi-k3@v1")

    assert context_for(approval, chat_busy=True) == "modal"
    assert context_for(Screen(), chat_busy=True) == "running"
    assert context_for(Screen(), chat_busy=False) == "idle"
    rendered = render_hints(
        "modal", keys.default_map(), translator.t, width=120, modal_hints=approval.HINT_KEYS
    )
    assert "A Разрешить один раз" in rendered
    assert "V вся команда" in rendered
    assert "keys.action." not in rendered


def test_help_modal_hints_are_localized() -> None:
    translator = Translator("en")
    help_screen = HelpScreen(translator, keys.default_map())

    rendered = render_hints(
        "modal",
        keys.default_map(),
        translator.t,
        width=120,
        modal_hints=help_screen.HINT_KEYS,
    )

    assert "Pagedown next page" in rendered
    assert "Escape close" in rendered


def test_palette_session_group_is_translated_in_both_locales() -> None:
    for locale, label in (("en", "Session"), ("ru", "Сессия")):
        assert Translator(locale).t("palette.group.session") == label
