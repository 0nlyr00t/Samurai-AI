import re
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, field_validator

_IPAddress = IPv4Address | IPv6Address
_IPNetwork = IPv4Network | IPv6Network
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_DEFAULT_PORTS = {"http": 80, "https": 443}
# a model writes `localhost` for the loopback address it just read from `port`:
# both spellings must mean the same host, or every local run trips the scope
_LOOPBACK_ALIASES = {"localhost": "127.0.0.1", "ip6-localhost": "::1"}


class ScopeValidationError(ValueError):
    pass


class ScopeDecisionReason(StrEnum):
    ALLOWED = "allowed"
    EXCLUDED = "excluded"
    OUT_OF_SCOPE = "out-of-scope"
    INVALID_TARGET = "invalid-target"


class _RuleKind(StrEnum):
    DOMAIN = "domain"
    WILDCARD = "wildcard"
    IP = "ip"
    NETWORK = "network"
    URL = "url"


@dataclass(frozen=True)
class _Rule:
    kind: _RuleKind
    text: str
    host: str | None = None
    scheme: str | None = None
    port: int | None = None
    address: _IPAddress | None = None
    network: _IPNetwork | None = None


@dataclass(frozen=True)
class _Target:
    text: str
    host: str
    is_url: bool
    scheme: str | None = None
    port: int | None = None
    address: _IPAddress | None = None


class ScopeDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: ScopeDecisionReason
    target: str
    matched_rule: str | None = None
    detail: str


class ScopeContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    includes: tuple[str, ...]
    excludes: tuple[str, ...] = ()

    @field_validator("includes")
    @classmethod
    def validate_includes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        rules = _normalize_rules(value)
        if not rules:
            raise ScopeValidationError("at least one include rule is required")
        return rules

    @field_validator("excludes")
    @classmethod
    def validate_excludes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_rules(value)

    def evaluate(self, raw_target: str) -> ScopeDecision:
        try:
            target = _parse_target(raw_target)
        except ScopeValidationError as exc:
            return ScopeDecision(
                allowed=False,
                reason=ScopeDecisionReason.INVALID_TARGET,
                target=raw_target,
                detail=str(exc),
            )

        target = _with_loopback_alias(target)
        for raw_rule in self.excludes:
            rule = _parse_rule(raw_rule)
            if _matches(rule, target):
                return ScopeDecision(
                    allowed=False,
                    reason=ScopeDecisionReason.EXCLUDED,
                    target=target.text,
                    matched_rule=rule.text,
                    detail=f"target matches exclusion {rule.text}",
                )

        for raw_rule in self.includes:
            rule = _parse_rule(raw_rule)
            if _matches(rule, target):
                return ScopeDecision(
                    allowed=True,
                    reason=ScopeDecisionReason.ALLOWED,
                    target=target.text,
                    matched_rule=rule.text,
                    detail=f"target matches inclusion {rule.text}",
                )

        return ScopeDecision(
            allowed=False,
            reason=ScopeDecisionReason.OUT_OF_SCOPE,
            target=target.text,
            detail="target does not match any inclusion rule",
        )


def _with_loopback_alias(target: _Target) -> _Target:
    """Treat `localhost` and `127.0.0.1` as the same host on both sides."""
    alias = _LOOPBACK_ALIASES.get(target.host)
    if alias is not None:
        return _Target(
            text=target.text.replace(target.host, alias),
            host=alias,
            is_url=target.is_url,
            scheme=target.scheme,
            port=target.port,
            address=_address_of(alias),
        )
    return target


def _normalize_rules(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        rule = _parse_rule(value)
        if rule.text not in seen:
            normalized.append(rule.text)
            seen.add(rule.text)
    return tuple(normalized)


def _parse_rule(raw_rule: str) -> _Rule:
    raw = raw_rule.strip()
    if not raw:
        raise ScopeValidationError("scope rule cannot be empty")
    if any(character.isspace() for character in raw):
        raise ScopeValidationError(f"scope rule cannot contain whitespace: {raw_rule!r}")

    if "://" in raw:
        return _parse_url_rule(raw)

    if raw.startswith("*."):
        host = _normalize_domain(raw[2:])
        return _Rule(kind=_RuleKind.WILDCARD, text=f"*.{host}", host=host)

    if "/" in raw:
        try:
            network = ip_network(raw, strict=True)
        except ValueError as exc:
            raise ScopeValidationError(f"invalid or non-canonical network rule: {raw}") from exc
        return _Rule(kind=_RuleKind.NETWORK, text=str(network), network=network)

    try:
        parsed = ip_address(raw)
    except ValueError:
        host = _normalize_domain(raw)
        return _Rule(kind=_RuleKind.DOMAIN, text=host, host=host)
    address = _collapse_mapped(parsed)
    return _Rule(kind=_RuleKind.IP, text=str(address), address=address)


def _parse_url_rule(raw: str) -> _Rule:
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise ScopeValidationError("invalid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ScopeValidationError("only http and https URL scope rules are supported")
    if parts.username is not None or parts.password is not None:
        raise ScopeValidationError("credentials in URL scope rules are not allowed")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ScopeValidationError("URL scope rules currently support origins only")
    if not parts.hostname:
        raise ScopeValidationError("URL scope rule must contain a hostname")

    host = _normalize_host(parts.hostname)
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise ScopeValidationError("invalid URL port") from exc

    text = _canonical_url(scheme, host, port)
    return _Rule(kind=_RuleKind.URL, text=text, host=host, scheme=scheme, port=port)


def _parse_target(raw_target: str) -> _Target:
    raw = raw_target.strip()
    if not raw:
        raise ScopeValidationError("target cannot be empty")
    if any(character.isspace() for character in raw):
        raise ScopeValidationError("target cannot contain whitespace")

    if "://" not in raw:
        if "/" in raw or raw.startswith("*."):
            raise ScopeValidationError("target must be a domain, IP address, or HTTP(S) URL")
        host = _normalize_host(raw)
        return _Target(text=host, host=host, is_url=False, address=_address_of(host))

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise ScopeValidationError("invalid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ScopeValidationError("only http and https targets are supported")
    if parts.username is not None or parts.password is not None:
        raise ScopeValidationError("credentials in targets are not allowed")
    if not parts.hostname:
        raise ScopeValidationError("target URL must contain a hostname")

    host = _normalize_host(parts.hostname)
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise ScopeValidationError("invalid target URL port") from exc

    netloc = _format_netloc(host, scheme, port)
    text = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    return _Target(
        text=text,
        host=host,
        is_url=True,
        scheme=scheme,
        port=port,
        address=_address_of(host),
    )


def _collapse_mapped(address: _IPAddress) -> _IPAddress:
    """Fold an IPv4-mapped IPv6 address to its IPv4 form.

    ``::ffff:198.51.100.7`` reaches the same host as ``198.51.100.7``; without
    folding, a v4 scope rule would not cover the mapped spelling a URL or a
    resolver can legitimately produce.
    """
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _address_of(host: str) -> _IPAddress | None:
    try:
        return _collapse_mapped(ip_address(host))
    except ValueError:
        return None


def _normalize_host(raw_host: str) -> str:
    raw = raw_host.strip().rstrip(".")
    if not raw or "%" in raw:
        raise ScopeValidationError(f"invalid hostname: {raw_host!r}")
    try:
        return str(ip_address(raw))
    except ValueError:
        return _normalize_domain(raw)


def _normalize_domain(raw_domain: str) -> str:
    raw = raw_domain.strip().rstrip(".")
    if not raw:
        raise ScopeValidationError("domain cannot be empty")
    try:
        domain = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ScopeValidationError(f"invalid internationalized domain: {raw_domain}") from exc
    if len(domain) > 253:
        raise ScopeValidationError("domain is too long")
    labels = domain.split(".")
    if any(not _LABEL.fullmatch(label) for label in labels):
        raise ScopeValidationError(f"invalid domain: {raw_domain}")
    return domain


def _canonical_url(scheme: str, host: str, port: int) -> str:
    return urlunsplit((scheme, _format_netloc(host, scheme, port), "", "", ""))


def _format_netloc(host: str, scheme: str, port: int) -> str:
    formatted_host = f"[{host}]" if ":" in host else host
    if port == _DEFAULT_PORTS[scheme]:
        return formatted_host
    return f"{formatted_host}:{port}"


def _matches(rule: _Rule, target: _Target) -> bool:
    rule_host = _LOOPBACK_ALIASES.get(rule.host or "", rule.host)
    if rule.kind == _RuleKind.DOMAIN:
        # an IP rule and a URL host can meet through the loopback alias, so the
        # comparison is on the host text, not on "is it an address"
        return target.host == rule_host
    if rule.kind == _RuleKind.WILDCARD:
        # ``*.example.com`` covers the apex as well as every subdomain: a
        # program that publishes a wildcard means the bare domain too.
        return target.address is None and (
            target.host == rule_host or target.host.endswith(f".{rule_host}")
        )
    if rule.kind == _RuleKind.IP:
        return target.address == rule.address
    if rule.kind == _RuleKind.NETWORK:
        return (
            target.address is not None
            and rule.network is not None
            and target.address.version == rule.network.version
            and target.address in rule.network
        )
    return (
        target.is_url
        and target.scheme == rule.scheme
        and target.host == rule_host
        and target.port == rule.port
    )
