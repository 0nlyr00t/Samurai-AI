"""The central guard: host-shaped literals out of any parameter, fail closed."""

import pytest

from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.scope_guard import ScopeGuardError, guard

SCOPE = ScopeContract(includes=("10.10.0.0/24", "example.com", "*.example.com"))


def test_a_host_inside_the_scope_passes() -> None:
    guard(SCOPE, {"target": "10.10.0.5", "url": "https://api.example.com/x"})


def test_an_outside_host_is_refused() -> None:
    with pytest.raises(ScopeGuardError, match="outside the operation scope"):
        guard(SCOPE, {"target": "10.10.9.9"})


def test_a_second_value_in_a_list_is_checked() -> None:
    with pytest.raises(ScopeGuardError, match="evil.com"):
        guard(SCOPE, {"targets": ["example.com", "evil.com"]})


def test_host_and_port_are_read_as_the_host() -> None:
    guard(SCOPE, {"host": "example.com:8443"})
    with pytest.raises(ScopeGuardError):
        guard(SCOPE, {"host": "evil.com:8443"})


def test_a_network_wider_than_the_scope_is_refused() -> None:
    guard(SCOPE, {"targets": "10.10.0.0/25"})
    with pytest.raises(ScopeGuardError, match="not inside any included network"):
        guard(SCOPE, {"targets": "10.10.0.0/16"})


def test_an_exempt_parameter_is_not_checked() -> None:
    with pytest.raises(ScopeGuardError):
        guard(SCOPE, {"resolver": "https://dns.google/dns-query"})
    guard(SCOPE, {"resolver": "https://dns.google/dns-query"}, exempt=("resolver",))


def test_local_paths_are_not_hosts() -> None:
    guard(SCOPE, {"wordlist": "/usr/share/wordlists/common.txt"})
    guard(SCOPE, {"wordlist": "assets/wordlists/common.txt"})


def test_plain_words_and_numbers_are_not_hosts() -> None:
    guard(SCOPE, {"profile": "fast", "workers": 40, "extensions": ".php,.bak"})


def test_the_message_names_the_parameter() -> None:
    with pytest.raises(ScopeGuardError, match="parameter 'target'"):
        guard(SCOPE, {"target": "8.8.8.8"})
