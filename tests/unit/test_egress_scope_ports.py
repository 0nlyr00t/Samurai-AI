"""The proxy's port gate follows the scope, or a named port is authorized in vain."""

from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.egress import DEFAULT_ALLOWED_PORTS, scope_ports


def test_web_ports_are_always_allowed() -> None:
    assert scope_ports(ScopeContract(includes=("example.com",))) == DEFAULT_ALLOWED_PORTS


def test_a_port_named_in_a_rule_is_allowed_too() -> None:
    scope = ScopeContract(includes=("http://box.internal:3000", "https://other:8443"))
    assert {3000, 8443} <= scope_ports(scope)


def test_a_bare_host_adds_nothing() -> None:
    assert scope_ports(ScopeContract(includes=("10.0.0.5", "example.com"))) == (
        DEFAULT_ALLOWED_PORTS
    )


def test_an_excluded_origin_keeps_its_port_open_for_the_default_check() -> None:
    """An exclusion on a port should not turn into a port denial elsewhere."""
    scope = ScopeContract(includes=("example.com",), excludes=("http://example.com:8080",))
    assert 8080 in scope_ports(scope)
