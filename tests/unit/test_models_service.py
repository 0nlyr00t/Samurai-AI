"""Model service settings: the request timeout."""

from __future__ import annotations

from pathlib import Path

from samurai_ai.cli import _make_services


def test_the_model_timeout_comes_from_settings_and_is_bounded(tmp_path: Path) -> None:
    """60 s ended real runs on slow gateways; the operator can raise it."""
    services = _make_services(tmp_path / "t.db")
    models = services[7]
    assert models.request_timeout() == 180.0
    assert models.settings is not None
    models.settings.set_setting("model.timeout_seconds", "420")
    assert models.request_timeout() == 420.0
    models.settings.set_setting("model.timeout_seconds", "99999")
    assert models.request_timeout() == 600.0
    models.settings.set_setting("model.timeout_seconds", "2")
    assert models.request_timeout() == 10.0


def test_a_handwritten_prlctl_command_is_requoted(tmp_path: Path) -> None:
    """`prlctl exec <uuid> curl ...` typed by the model must not lose its tail."""
    from samurai_ai.tui.chat import ChatController

    # the helper is a pure function of the command; call it unbound
    normalize = ChatController._normalize_prlctl
    out = normalize(None, "prlctl exec {1381d6d4-31a6-4aee-ab87-4193b12e98ae} curl -s http://10.0.0.5/")
    assert out.startswith("prlctl exec ")
    assert "curl" in out and "http://10.0.0.5/" in out
    # already-quoted or unrelated commands pass through unchanged
    plain = normalize(None, "ls -la /tmp")
    assert plain == "ls -la /tmp"
