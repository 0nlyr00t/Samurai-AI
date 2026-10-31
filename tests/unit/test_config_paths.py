"""State directory resolution: one place, XDG by default, env overrides on top."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai import config


def test_state_lives_under_the_xdg_config_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMURAI_HOME", raising=False)
    monkeypatch.delenv("SAMURAI_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/config-home")
    assert config.config_directory() == Path("/tmp/config-home/samurai")
    assert config.default_database_path() == Path("/tmp/config-home/samurai/samurai.db")


def test_home_config_is_the_default_without_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMURAI_HOME", raising=False)
    monkeypatch.delenv("SAMURAI_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", "/tmp/home")
    assert config.config_directory() == Path("/tmp/home/.config/samurai")


def test_every_file_shares_one_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMURAI_HOME", raising=False)
    monkeypatch.delenv("SAMURAI_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/one")
    directory = config.config_directory()
    assert config.vault_key_path().parent == directory
    assert config.sandbox_allowlist_path().parent == directory


def test_samurai_home_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMURAI_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SAMURAI_HOME", "/tmp/explicit")
    assert config.config_directory() == Path("/tmp/explicit")


def test_old_config_home_spelling_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMURAI_HOME", raising=False)
    monkeypatch.setenv("SAMURAI_CONFIG_HOME", "/tmp/legacy-spelling")
    assert config.config_directory() == Path("/tmp/legacy-spelling")


