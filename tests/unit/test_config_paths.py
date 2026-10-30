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
    assert config.is_overridden() is True


def test_old_config_home_spelling_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMURAI_HOME", raising=False)
    monkeypatch.setenv("SAMURAI_CONFIG_HOME", "/tmp/legacy-spelling")
    assert config.config_directory() == Path("/tmp/legacy-spelling")


def test_legacy_directory_is_reported_only_when_it_holds_a_database(
    tmp_path: Path,
) -> None:
    assert config.legacy_directory(tmp_path) is None
    (tmp_path / ".samurai").mkdir()
    assert config.legacy_directory(tmp_path) is None  # empty leftover is noise
    (tmp_path / ".samurai" / "samurai.db").write_text("")
    assert config.legacy_directory(tmp_path) == tmp_path / ".samurai"


def test_cli_warns_about_a_legacy_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from samurai_ai import cli

    monkeypatch.delenv("SAMURAI_HOME", raising=False)
    monkeypatch.delenv("SAMURAI_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".samurai").mkdir()
    (tmp_path / ".samurai" / "samurai.db").write_text("")
    cli._warn_legacy_state()
    assert ".samurai" in capsys.readouterr().err


def test_cli_warning_stays_quiet_when_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from samurai_ai import cli

    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".samurai").mkdir()
    (tmp_path / ".samurai" / "samurai.db").write_text("")
    cli._warn_legacy_state()
    assert capsys.readouterr().err == ""
