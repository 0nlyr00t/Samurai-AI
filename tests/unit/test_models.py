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
