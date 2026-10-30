import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Keep themes and key bindings out of the developer's real config directory."""
    monkeypatch.setenv("SAMURAI_CONFIG_HOME", str(tmp_path / "config"))
