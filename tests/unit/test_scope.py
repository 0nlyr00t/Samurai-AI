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


def test_wildcard_covers_apex_and_subdomains() -> None:
    scope = ScopeContract(includes=("*.example.com",))

    assert scope.evaluate("www.example.com").allowed
    assert scope.evaluate("deep.www.example.com").allowed
    # a published wildcard means the bare domain too
    assert scope.evaluate("example.com").allowed
    assert not scope.evaluate("notexample.com").allowed
    assert not scope.evaluate("example.com.evil.test").allowed


def test_ipv4_mapped_ipv6_is_the_same_target_as_ipv4() -> None:
    scope = ScopeContract(includes=("198.51.100.7",))

    assert scope.includes == ("198.51.100.7",)
    assert scope.evaluate("::ffff:198.51.100.7").allowed
    assert scope.evaluate("http://[::ffff:198.51.100.7]/").allowed
    assert scope.evaluate("https://[::FFFF:C633:6407]:8443/x").allowed


def test_ipv4_mapped_rule_covers_ipv4_target() -> None:
    scope = ScopeContract(includes=("::ffff:198.51.100.7",))

    assert scope.includes == ("198.51.100.7",)
    assert scope.evaluate("198.51.100.7").allowed
    assert not scope.evaluate("198.51.100.8").allowed


def test_mapped_address_still_honours_excludes() -> None:
    scope = ScopeContract(includes=("198.51.100.0/24",), excludes=("198.51.100.7",))

    denied = scope.evaluate("::ffff:198.51.100.7")
    assert not denied.allowed
    assert denied.reason == ScopeDecisionReason.EXCLUDED
    assert scope.evaluate("::ffff:198.51.100.8").allowed


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
    assert scope.evaluate("пример.рф").allowed


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


def test_localhost_and_loopback_are_the_same_host() -> None:
    """A model writes `localhost` for the port it just read; that is in scope."""
    contract = ScopeContract(includes=("127.0.0.1",))
    assert contract.evaluate("http://localhost:3000/").allowed
    assert contract.evaluate("localhost").allowed
    # and the rule may be written the other way round
    assert ScopeContract(includes=("localhost",)).evaluate("http://127.0.0.1:8080").allowed
    # an address is still not a domain
    assert not ScopeContract(includes=("example.com",)).evaluate("10.0.0.1").allowed
