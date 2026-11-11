from __future__ import annotations

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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    #: name → IP (or domain) for targets that only resolve on the target's own
    #: network: an HTB vhost, a box's internal DNS name. Both scope matching and
    #: the sandbox consult this, so `flight.htb` behaves like the address it names.
    hosts: dict[str, str] = Field(default_factory=dict)
    #: plumbing that is never a target: resolvers, the platform API of the mode.
    #: Not operator data — execution injects it per mode so both the adapter's own
    #: check and the central guard let `api.hackthebox.com` through without the
    #: contract having to authorize a host nobody attacks.
    infra: tuple[str, ...] = ()

    def with_infra(self, infra: frozenset[str] | tuple[str, ...]) -> ScopeContract:
        """Same contract plus the mode's plumbing hosts."""
        if not infra:
            return self
        return self.model_copy(update={"infra": tuple(sorted(infra))})

    @field_validator("includes")
    @classmethod
    def validate_includes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # an empty include list is a valid, fully denied contract: excluding
        # the last target must not be impossible just to keep a placeholder
        return _normalize_rules(value)

    @field_validator("excludes")
    @classmethod
    def validate_excludes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_rules(value)

    @field_validator("hosts")
    @classmethod
    def validate_hosts(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for name, target in value.items():
            alias = _normalize_host(name)
            if _address_of(alias) is not None:
                raise ScopeValidationError(
                    f"host alias must be a name, not an address: {name!r}"
                )
            normalized[alias] = _parse_target(target).host
        return normalized

    def resolve_host(self, host: str) -> str:
        """The host a target actually means, following one alias hop."""
        return self.hosts.get(_normalize_host(host), host)

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

        target = _with_loopback_alias(_with_alias(self, target))
        if self._is_infra(target.text):
            return ScopeDecision(
                allowed=True,
                reason=ScopeDecisionReason.ALLOWED,
                target=target.text,
                detail="service host (resolver or platform API), not a target",
            )
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

    def excluded(self, raw_target: str) -> bool:
        """Whether an exclusion matches this target with evaluate's semantics."""
        text = raw_target.strip()
        try:
            target = _parse_target(text)
        except ScopeValidationError:
            # a wildcard is a rule shape, not a hostname: only an identical
            # exclusion is meaningful; do not accidentally block its children
            return text in self.excludes
        for raw_rule in self.excludes:
            try:
                rule = _parse_rule(raw_rule)
            except ScopeValidationError:
                continue
            if _matches(rule, target):
                return True
        return False

    def covers(self, raw_target: str) -> bool:
        """Does the contract already authorize this target without changing scope?

        `evaluate` answers "may I touch it", but adding a rule asks the narrower
        question "was it already reachable": only a genuine widening deserves a
        confirmation prompt, and repeating a rule deserves none.
        """
        if self.excluded(raw_target):
            return False
        candidate_text = raw_target.strip()
        if self.hosts.get(candidate_text) is not None:
            return True
        try:
            candidate = _parse_rule(candidate_text)
        except ScopeValidationError:
            return False
        for raw_rule in self.includes:
            try:
                rule = _parse_rule(raw_rule)
            except ScopeValidationError:
                continue
            if _rule_covers(rule, candidate):
                return True
        return False

    def _is_infra(self, raw: str) -> bool:
        """A suffix entry («.hackthebox.com») covers any subdomain of the plumbing."""
        if not self.infra:
            return False
        host = raw.strip().strip("'\"[]").lower()
        if "://" in host:
            host = urlsplit(host).hostname or host
        host = host.split("/", 1)[0].split(":", 1)[0].lower()
        if not host:
            return False
        return any(
            host == entry or (entry.startswith(".") and host.endswith(entry))
            for entry in self.infra
        )

    def evaluate_network(self, raw_network: str) -> ScopeDecision:
        """Judge a whole network argument: it must fit inside the authorized scope.

        A tool that takes a range (a sweep, a scan over a subnet) can otherwise widen
        a /32 authorization to a /16 just by its argument.
        """
        try:
            parsed_network = ip_network(raw_network.strip(), strict=False)
        except ValueError as exc:
            return ScopeDecision(
                allowed=False,
                reason=ScopeDecisionReason.INVALID_TARGET,
                target=raw_network,
                detail=f"not a network: {exc}",
            )
        network: _IPNetwork = parsed_network
        text = str(network)

        for raw_rule in self.excludes:
            rule = _parse_rule(raw_rule)
            if rule.kind is _RuleKind.NETWORK and rule.network is not None:
                if _inside(rule.network, network) or _inside(network, rule.network):
                    return ScopeDecision(
                        allowed=False,
                        reason=ScopeDecisionReason.EXCLUDED,
                        target=text,
                        matched_rule=rule.text,
                        detail=f"network overlaps exclusion {rule.text}",
                    )

        for raw_rule in self.includes:
            rule = _parse_rule(raw_rule)
            if rule.kind is _RuleKind.NETWORK and rule.network is not None:
                if _inside(network, rule.network):
                    return ScopeDecision(
                        allowed=True,
                        reason=ScopeDecisionReason.ALLOWED,
                        target=text,
                        matched_rule=rule.text,
                        detail=f"network fits inside {rule.text}",
                    )
            if rule.kind is _RuleKind.IP and rule.address is not None:
                if network.num_addresses == 1 and network.network_address == rule.address:
                    return ScopeDecision(
                        allowed=True,
                        reason=ScopeDecisionReason.ALLOWED,
                        target=text,
                        matched_rule=rule.text,
                        detail=f"single address matches {rule.text}",
                    )

        return ScopeDecision(
            allowed=False,
            reason=ScopeDecisionReason.OUT_OF_SCOPE,
            target=text,
            detail=f"network {text} is not inside any included network",
        )


def _inside(inner: _IPNetwork, outer: _IPNetwork) -> bool:
    """Is ``inner`` fully inside ``outer``? (``subnet_of`` trips the union in mypy.)"""
    if inner.version != outer.version:
        return False
    return inner.prefixlen >= outer.prefixlen and inner.network_address in outer


def _with_alias(contract: ScopeContract, target: _Target) -> _Target:
    """An aliased name is judged as the address it stands for."""
    mapped = contract.hosts.get(target.host)
    if mapped is None:
        return target
    return _Target(
        text=target.text.replace(target.host, mapped),
        host=mapped,
        is_url=target.is_url,
        scheme=target.scheme,
        port=target.port,
        address=_address_of(mapped),
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


def _rule_covers(rule: _Rule, candidate: _Rule) -> bool:
    """Is `candidate` a subset of the authorization `rule` already grants?"""
    if rule.text == candidate.text:
        return True
    if rule.kind is _RuleKind.NETWORK:
        if candidate.kind is _RuleKind.IP and candidate.address is not None:
            return candidate.address in rule.network  # type: ignore[operator]
        if candidate.kind is _RuleKind.NETWORK and candidate.network is not None:
            return candidate.network.subnet_of(rule.network)  # type: ignore[arg-type]
        return False
    if rule.kind is _RuleKind.IP and candidate.kind is _RuleKind.NETWORK:
        # `1.2.3.4` and `1.2.3.4/32` name the same thing; a wider network is news
        return (
            candidate.network is not None
            and candidate.network.prefixlen == candidate.network.max_prefixlen
            and candidate.network.network_address == rule.address
        )
    if rule.kind in {_RuleKind.DOMAIN, _RuleKind.WILDCARD}:
        if candidate.kind not in {_RuleKind.DOMAIN, _RuleKind.WILDCARD}:
            return False
        if rule.host is None or candidate.host is None:
            return False
        # a domain rule reaches everything below it, an IP or URL rule does not
        under = candidate.host.endswith("." + rule.host)
        if rule.kind is _RuleKind.DOMAIN:
            return under
        return under or rule.host == candidate.host
    return False


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


def _registrable_domain(host: str) -> str:
    """The last two labels of a domain, e.g. a.b.scaffold.htb -> scaffold.htb.

    A heuristic, not a public-suffix lookup: enough to tell "same site" for the
    scope agent's relatedness check, where a loose match only widens the pool the
    model then judges, never grants access on its own.
    """
    labels = host.strip().strip(".").lower().split(".")
    if len(labels) < 2:
        return ""
    return ".".join(labels[-2:])


def _same_24(left: _IPAddress, right: _IPAddress) -> bool:
    if type(left) is not type(right):
        return False
    prefix = 24 if isinstance(left, IPv4Address) else 120
    return ip_network(f"{left}/{prefix}", strict=False) == ip_network(
        f"{right}/{prefix}", strict=False
    )


def scope_relatedness(
    scope: ScopeContract, target: str, *, finding_text: str = "", goal: str = ""
) -> list[str]:
    """Why a target is close to the current scope, for the scope agent.

    Returns a human reason per signal, empty when the target is unrelated. This
    is the *moderate* boundary of autonomous widening: same /24 or same
    registrable domain as an in-scope rule, a mention in confirmed findings or
    loot, or a home inside the operation's stated goal range. Excluded targets
    are the caller's hard stop and are not weighed here. The list only decides
    whether the model is consulted; it never adds scope by itself.
    """
    try:
        parsed = _parse_target(target)
    except ScopeValidationError:
        return []
    reasons: list[str] = []
    address = parsed.address
    if address is not None:
        for rule in scope.includes:
            try:
                network = ip_network(rule, strict=False)
            except ValueError:
                rule_address = _address_of(rule)
                if rule_address is not None and _same_24(address, rule_address):
                    reasons.append(f"same /24 as {rule}")
                continue
            if address in network or _same_24(address, network.network_address):
                reasons.append(f"same /24 as {rule}")
    else:
        registrable = _registrable_domain(parsed.host)
        if registrable:
            for rule in scope.includes:
                bare = rule[2:] if rule.startswith("*.") else rule
                if _address_of(bare) is None and _registrable_domain(bare) == registrable:
                    reasons.append(f"same domain as {rule}")
    haystack = finding_text.lower()
    if haystack and (parsed.host in haystack or parsed.text in haystack):
        reasons.append("named in a confirmed finding or loot")
    if goal:
        for token in re.split(r"[\s,;]+", goal):
            try:
                network = ip_network(token.strip(), strict=False)
            except ValueError:
                continue
            if address is not None and address in network:
                reasons.append(f"inside the goal range {token.strip()}")
                break
    return list(dict.fromkeys(reasons))
