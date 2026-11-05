"""Tool keys: stored under the lower-case vault name, injected by env name."""

import pytest

from samurai_ai.domain.vault import VaultError
from samurai_ai.services import tool_keys as tk


class FakeVault:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def exists(self, name: str, operation_id=None) -> bool:
        return name in self.store

    def get(self, name: str, operation_id=None) -> str:
        if name not in self.store:
            raise VaultError(f"secret not found: {name!r}")
        return self.store[name]

    def add(self, name, value, operation_id=None, *, created_by="operator"):
        self.store[name] = value

    def set(self, name, value, operation_id=None):
        self.store[name] = value

    def remove(self, name, operation_id=None):
        self.store.pop(name, None)


def test_env_names_map_to_lower_case_vault_names() -> None:
    assert tk.vault_name("SHODAN_API_KEY") == "shodan_api_key"


def test_setting_and_clearing_a_key() -> None:
    vault = FakeVault()
    tk.set_key(vault, "SHODAN_API_KEY", "value-1")
    assert tk.key_state(vault, "SHODAN_API_KEY") == "set"
    tk.set_key(vault, "SHODAN_API_KEY", "value-2")
    assert vault.store["shodan_api_key"] == "value-2"
    tk.set_key(vault, "SHODAN_API_KEY", "")
    assert tk.key_state(vault, "SHODAN_API_KEY") == "empty"


def test_only_declared_names_are_injected() -> None:
    vault = FakeVault()
    tk.set_key(vault, "SHODAN_API_KEY", "s")
    tk.set_key(vault, "UNRELATED_TOKEN", "u")
    assert tk.inject(("SHODAN_API_KEY",), {}, vault) == {"SHODAN_API_KEY": "s"}


def test_a_value_from_the_call_wins() -> None:
    vault = FakeVault()
    tk.set_key(vault, "SHODAN_API_KEY", "stored")
    assert tk.inject(("SHODAN_API_KEY",), {"SHODAN_API_KEY": "given"}, vault) == {}


def test_missing_keys_are_simply_absent() -> None:
    assert tk.inject(("FEODO_KEY",), {}, FakeVault()) == {}
    assert tk.inject(("FEODO_KEY",), {}, None) == {}


def test_the_catalogue_entries_are_well_formed() -> None:
    for entry in tk.CATALOGUE:
        assert entry.env_name.isupper()
        assert entry.url.startswith("https://")
        assert tk.key_for(entry.env_name) is entry


def test_a_key_carries_the_tool_that_uses_it() -> None:
    assert tk.key_for("SHODAN_API_KEY").tool == "intel.shodan"
    assert tk.key_for("WPSCAN_API_KEY").tool == "scan.wpscan"
    assert tk.key_for("NOPE") is None


def test_stored_keys_reads_only_the_catalogue() -> None:
    vault = FakeVault()
    tk.set_key(vault, "SHODAN_API_KEY", "s")
    vault.store["stray"] = "x"
    assert tk.stored_keys(vault) == {"SHODAN_API_KEY": "s"}


@pytest.mark.parametrize("name", ["SHODAN_API_KEY", "CENSYS_API_KEY", "WPSCAN_API_KEY"])
def test_the_catalogue_covers_the_requested_providers(name: str) -> None:
    assert tk.key_for(name) is not None
