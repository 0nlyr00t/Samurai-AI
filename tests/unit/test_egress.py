import base64

import pytest

from samurai_ai.domain.execution import ToolExecutionError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.egress import (
    VERDICT_ALLOWED,
    VERDICT_DENIED_PORT,
    VERDICT_DENIED_SCOPE,
    EgressProxy,
    check_proxy_authorization,
    parse_request_target,
)

TOKEN = "ab" * 16


def test_proxy_auth_bearer() -> None:
    assert check_proxy_authorization(f"Bearer {TOKEN}", TOKEN)
    assert not check_proxy_authorization("Bearer wrong", TOKEN)
    assert not check_proxy_authorization(None, TOKEN)
    assert not check_proxy_authorization("", TOKEN)
    assert not check_proxy_authorization(f"bearer {TOKEN}x", TOKEN)


def test_proxy_auth_basic_password_is_token() -> None:
    encoded = base64.b64encode(f"samurai:{TOKEN}".encode()).decode()
    assert check_proxy_authorization(f"Basic {encoded}", TOKEN)
    wrong = base64.b64encode(b"samurai:nope").decode()
    assert not check_proxy_authorization(f"Basic {wrong}", TOKEN)
    assert not check_proxy_authorization("Basic !!!not-base64!!!", TOKEN)


def test_parse_connect_target() -> None:
    assert parse_request_target("CONNECT", "example.com:443") == (
        "https",
        "example.com",
        443,
        "",
    )
    with pytest.raises(ToolExecutionError, match="invalid CONNECT"):
        parse_request_target("CONNECT", "noport")
    with pytest.raises(ToolExecutionError, match="invalid CONNECT port"):
        parse_request_target("CONNECT", "example.com:https")


def test_parse_absolute_uri_target() -> None:
    assert parse_request_target("GET", "http://example.com/a?b=1") == (
        "http",
        "example.com",
        80,
        "/a?b=1",
    )
    assert parse_request_target("GET", "http://127.0.0.1:8080/") == (
        "http",
        "127.0.0.1",
        8080,
        "/",
    )
    with pytest.raises(ToolExecutionError, match="plain http"):
        parse_request_target("GET", "https://example.com/")
    with pytest.raises(ToolExecutionError, match="plain http"):
        parse_request_target("GET", "ftp://example.com/x")


def make_proxy(scope: ScopeContract, **overrides: object) -> EgressProxy:
    payload: dict[str, object] = {"scope": scope, "token": TOKEN}
    payload.update(overrides)
    return EgressProxy(**payload)  # type: ignore[arg-type]


def test_verdict_allowed_for_in_scope_host() -> None:
    proxy = make_proxy(ScopeContract(includes=("127.0.0.1",)))
    assert proxy._verdict("127.0.0.1", 80, "http") == VERDICT_ALLOWED
    assert proxy._verdict("127.0.0.1", 443, "https") == VERDICT_ALLOWED


def test_verdict_denied_port_policy() -> None:
    proxy = make_proxy(ScopeContract(includes=("127.0.0.1",)))
    assert proxy._verdict("127.0.0.1", 22, "https") == VERDICT_DENIED_PORT
    assert proxy._verdict("127.0.0.1", 8080, "http") == VERDICT_DENIED_PORT


def test_verdict_denied_out_of_scope_host() -> None:
    proxy = make_proxy(ScopeContract(includes=("example.com",)))
    assert proxy._verdict("other.example", 443, "https") == VERDICT_DENIED_SCOPE


def test_verdict_denied_when_resolved_address_excluded() -> None:
    proxy = make_proxy(
        ScopeContract(includes=("allowed.test",), excludes=("10.9.9.9",)),
        resolver=lambda host: ["10.9.9.9"],
    )
    assert proxy._verdict("allowed.test", 443, "https") == VERDICT_DENIED_SCOPE


def test_verdict_allowed_with_wildcard_scope() -> None:
    proxy = make_proxy(
        ScopeContract(includes=("*.example.com",)),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert proxy._verdict("api.example.com", 443, "https") == VERDICT_ALLOWED
    # the apex is covered by the wildcard too
    assert proxy._verdict("example.com", 443, "https") == VERDICT_ALLOWED
    assert proxy._verdict("example.org", 443, "https") == VERDICT_DENIED_SCOPE
