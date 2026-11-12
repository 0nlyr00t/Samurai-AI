"""The title smith tidies a model's answer into a short tab name."""

from __future__ import annotations

from samurai_ai.services import title_smith


def test_build_prompt_carries_the_task() -> None:
    system, user = title_smith.build_prompt("root the HTB box")
    assert "title" in system.lower()
    assert "root the HTB box" in user


def test_clean_strips_quotes_label_and_punctuation() -> None:
    assert title_smith.clean('"Root the HTB box"') == "Root the HTB box"
    assert title_smith.clean("Title: Root the HTB box") == "Root the HTB box"
    assert title_smith.clean("Root the HTB box.") == "Root the HTB box"
    # only the first line is a title; any prose after it is dropped
    assert title_smith.clean("Root the HTB box\nHere is why…") == "Root the HTB box"


def test_clean_clamps_a_long_answer() -> None:
    out = title_smith.clean("a " * 60, limit=20)
    assert len(out) <= 21 and out.endswith("…")


def test_clean_is_empty_for_blank_input() -> None:
    assert title_smith.clean("") == ""
    assert title_smith.clean("   \n  ") == ""
