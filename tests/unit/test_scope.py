import pytest
from pydantic import ValidationError

from samurai_ai.domain.scope import (
    ScopeContract,
    ScopeDecisionReason,
)


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


def test_a_host_alias_is_judged_as_its_address() -> None:
    """An HTB vhost stands for the box's IP: same verdict, and it resolves."""
    contract = ScopeContract(
        includes=("10.129.98.19",), hosts={"flight.htb": "10.129.98.19"}
    )
    assert contract.evaluate("http://flight.htb/").allowed
    assert contract.resolve_host("flight.htb") == "10.129.98.19"
    assert contract.resolve_host("other.htb") == "other.htb"


def test_a_host_alias_outside_scope_stays_out() -> None:
    contract = ScopeContract(
        includes=("10.129.98.19",), hosts={"elsewhere.htb": "10.10.10.10"}
    )
    assert not contract.evaluate("http://elsewhere.htb/").allowed


def test_a_host_alias_name_cannot_be_an_address() -> None:
    with pytest.raises(ValidationError, match="must be a name"):
        ScopeContract(includes=("10.0.0.1",), hosts={"10.0.0.2": "10.0.0.1"})


def test_a_network_must_fit_inside_the_scope() -> None:
    scope = ScopeContract(includes=("10.10.0.0/24",))
    assert scope.evaluate_network("10.10.0.0/24").allowed
    assert scope.evaluate_network("10.10.0.128/25").allowed
    # a wider network than the authorization is refused
    assert not scope.evaluate_network("10.10.0.0/16").allowed
    assert scope.evaluate_network("10.10.0.0/16").detail


def test_a_single_authorized_address_covers_only_itself() -> None:
    scope = ScopeContract(includes=("10.10.0.5",))
    assert scope.evaluate_network("10.10.0.5/32").allowed
    assert not scope.evaluate_network("10.10.0.0/24").allowed


def test_an_overlapping_exclusion_refuses_the_network() -> None:
    scope = ScopeContract(includes=("10.10.0.0/24",), excludes=("10.10.0.128/25",))
    assert not scope.evaluate_network("10.10.0.0/24").allowed
    assert scope.evaluate_network("10.10.0.0/25").allowed


def test_a_non_network_is_reported_as_invalid() -> None:
    decision = ScopeContract(includes=("10.10.0.0/24",)).evaluate_network("example.com")
    assert not decision.allowed and "not a network" in decision.detail


@pytest.mark.parametrize(
    ("include", "candidate"),
    [
        ("10.10.10.5", "10.10.10.5"),
        ("10.10.10.0/24", "10.10.10.7"),
        ("10.10.0.0/16", "10.10.11.0/24"),
        ("example.com", "api.example.com"),
        ("*.example.com", "a.b.example.com"),
        ("https://a.example.com:8443", "https://a.example.com:8443"),
    ],
)
def test_an_already_authorized_target_is_covered(include: str, candidate: str) -> None:
    """Widening is what deserves a prompt; a repeat or a subset does not."""
    assert ScopeContract(includes=(include,)).covers(candidate)


@pytest.mark.parametrize(
    ("include", "candidate"),
    [
        ("10.10.10.0/24", "10.10.11.0/24"),
        ("example.com", "notexample.com"),
        ("example.com", "example.org"),
        ("https://a.example.com:8443", "https://a.example.com:443"),
        ("10.10.10.5", "10.10.10.6"),
    ],
)
def test_a_target_outside_the_rule_is_not_covered(include: str, candidate: str) -> None:
    assert not ScopeContract(includes=(include,)).covers(candidate)


def test_an_alias_counts_as_covered_and_a_narrow_rule_is_no_widening() -> None:
    scope = ScopeContract(includes=("10.10.10.5",), hosts={"box.htb": "10.10.10.5"})
    assert scope.covers("box.htb")


def test_a_single_address_rule_does_not_cover_more_than_itself() -> None:
    """/32 must stay a /32: that is the whole point of adding one."""
    scope = ScopeContract(includes=("10.10.10.5",))
    assert scope.covers("10.10.10.5/32")
    assert not scope.covers("10.10.10.0/24")


def test_an_unparsable_target_is_not_covered() -> None:
    assert not ScopeContract(includes=("example.com",)).covers("not a target")
