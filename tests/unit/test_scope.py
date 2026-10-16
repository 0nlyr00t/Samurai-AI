import pytest
from pydantic import ValidationError

from samurai_ai.domain.scope import ScopeContract, ScopeDecisionReason


def test_domain_wildcard_and_exclusion_priority() -> None:
    scope = ScopeContract(
        includes=("*.Example.COM.", "example.com"),
        excludes=("api.example.com",),
    )

    assert scope.includes == ("*.example.com", "example.com")
    assert scope.evaluate("example.com").allowed
    assert scope.evaluate("www.example.com").allowed

    excluded = scope.evaluate("https://API.example.com/login")
    assert not excluded.allowed
    assert excluded.reason == ScopeDecisionReason.EXCLUDED
    assert excluded.matched_rule == "api.example.com"

    assert scope.evaluate("deep.api.example.com").allowed
    denied = scope.evaluate("example.org")
    assert denied.reason == ScopeDecisionReason.OUT_OF_SCOPE


def test_wildcard_does_not_include_apex_domain() -> None:
    scope = ScopeContract(includes=("*.example.com",))

    assert scope.evaluate("www.example.com").allowed
    assert scope.evaluate("deep.www.example.com").allowed
    assert not scope.evaluate("example.com").allowed


def test_url_origin_rule_checks_scheme_host_and_effective_port() -> None:
    scope = ScopeContract(includes=("HTTPS://Example.com:443",))

    assert scope.includes == ("https://example.com",)
    assert scope.evaluate("https://example.com/path?x=1").allowed
    assert scope.evaluate("https://example.com:443").allowed
    assert not scope.evaluate("http://example.com").allowed
    assert not scope.evaluate("https://example.com:444").allowed
    assert not scope.evaluate("https://www.example.com").allowed


def test_domain_rule_matches_http_urls_on_any_port() -> None:
    scope = ScopeContract(includes=("example.com",))

    decision = scope.evaluate("https://Example.COM:444/a?x=1")
    assert decision.allowed
    assert decision.target == "https://example.com:444/a?x=1"


def test_ip_cidr_ipv6_and_url_targets() -> None:
    scope = ScopeContract(
        includes=("192.0.2.0/24", "2001:db8::1"),
        excludes=("192.0.2.10",),
    )

    assert scope.evaluate("192.0.2.9").allowed
    assert scope.evaluate("https://192.0.2.9:8443/login").allowed
    assert scope.evaluate("https://[2001:db8::1]/").allowed
    assert not scope.evaluate("2001:db8::2").allowed
    assert scope.evaluate("192.0.2.10").reason == ScopeDecisionReason.EXCLUDED
    assert not scope.evaluate("198.51.100.1").allowed


def test_idna_rules_and_targets_are_normalized() -> None:
    scope = ScopeContract(includes=("*.пример.рф",))

    assert scope.includes[0].startswith("*.xn--")
    assert scope.evaluate("https://test.пример.рф/").allowed
    assert not scope.evaluate("пример.рф").allowed


@pytest.mark.parametrize(
    "rules",
    [
        (),
        ("*.",),
        ("192.0.2.1/24",),
        ("ftp://example.com",),
        ("https://example.com/path",),
        ("bad rule",),
    ],
)
def test_invalid_scope_rules_are_rejected(rules: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        ScopeContract(includes=rules)


@pytest.mark.parametrize(
    "target",
    [
        "",
        "bad target",
        "example.com/path",
        "ftp://example.com",
        "https://user@example.com",
        "http://[::1",
    ],
)
def test_invalid_targets_fail_closed(target: str) -> None:
    scope = ScopeContract(includes=("*.example.com", "example.com"))

    decision = scope.evaluate(target)
    assert not decision.allowed
    assert decision.reason == ScopeDecisionReason.INVALID_TARGET


def test_duplicate_rules_are_canonicalized() -> None:
    scope = ScopeContract(includes=("Example.com.", "example.com"))

    assert scope.includes == ("example.com",)
