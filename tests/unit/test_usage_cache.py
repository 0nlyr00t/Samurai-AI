"""Prefix-cache visibility: parsed from the gateway, shown, never invented."""

from __future__ import annotations

from samurai_ai.agents.openai_compat import _cached_tokens
from samurai_ai.agents.provider import Usage


def test_the_share_is_a_ratio_of_the_prompt() -> None:
    assert Usage(prompt_tokens=1000, cached_tokens=400).cache_hit == 0.4
    assert Usage(prompt_tokens=1000, cached_tokens=2000).cache_hit == 1.0
    assert Usage(prompt_tokens=1000).cache_hit == 0.0
    assert Usage(cached_tokens=10).cache_hit == 0.0


def test_each_gateway_spelling_is_read() -> None:
    assert _cached_tokens({"prompt_tokens_details": {"cached_tokens": 12}}) == 12
    assert _cached_tokens({"cache_read_input_tokens": 7}) == 7
    assert _cached_tokens({"prompt_cache_hit_tokens": 9}) == 9
    # a gateway that does not cache says nothing and nothing is invented
    assert _cached_tokens({}) == 0
    assert _cached_tokens({"prompt_tokens_details": {"cached_tokens": "x"}}) == 0


def test_the_tool_block_is_shorter_early_and_complete_later() -> None:
    """Same tools either way — dropping one is how a finding goes unrecorded."""
    from samurai_ai.tui import chat as chat_module

    controller = chat_module.ChatController.__new__(chat_module.ChatController)
    controller._stage_label = "recon"
    early = controller._tool_specs()
    controller._stage_label = "privesc"
    late = controller._tool_specs()
    assert [spec.name for spec in early] == [spec.name for spec in late]
    early_chars = sum(len(spec.description) for spec in early)
    late_chars = sum(len(spec.description) for spec in late)
    assert early_chars < late_chars
    # every tool's parameters stay untouched
    for before, after in zip(early, late, strict=True):
        assert before.parameters == after.parameters
