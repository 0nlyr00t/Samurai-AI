"""OIDC device login and the role gate.

The gate is one call on the command path. Identity comes from the provider and
the role from its claim; when no provider is configured the install behaves as a
local admin, so upgrading does not lock anyone out mid-campaign.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from samurai_ai.domain.access import (
    LOCAL_PRINCIPAL_ROLE,
    DeviceCode,
    NotAuthenticatedError,
    Principal,
    ProviderError,
    Role,
    Session,
    require,
)
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.access import AccessRepository

ISSUER_KEY = "auth.issuer"
CLIENT_ID_KEY = "auth.client_id"
SCOPES_KEY = "auth.scopes"
CLAIM_KEY = "auth.role_claim"
DEFAULT_SCOPES = "openid profile email"
DEFAULT_CLAIM = "groups"
#: Where the tokens live; the database only keeps the session row.
ACCESS_SECRET = "oidc-access"
REFRESH_SECRET = "oidc-refresh"

#: How long fetched keys are trusted, and how far clocks may disagree.
JWKS_TTL_SECONDS = 600
CLOCK_SKEW_SECONDS = 30

Transport = Callable[[str, dict[str, Any] | None, dict[str, str]], tuple[int, dict[str, Any]]]


def _urllib_transport(
    url: str, body: dict[str, Any] | None, headers: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    payload = None
    all_headers = {"Accept": "application/json", **headers}
    if body is not None:
        payload = "&".join(f"{key}={value}" for key, value in body.items()).encode()
        all_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=payload, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


def _now() -> datetime:
    return datetime.now(UTC)


class AccessService:
    def __init__(
        self,
        settings: SettingsService,
        vault: VaultService,
        access: AccessRepository,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.access = access
        self.transport = transport or _urllib_transport
        self.sleep = sleep
        self._jwks: dict[str, Any] = {}
        self._jwks_at = 0.0

    # --- configuration ----------------------------------------------------
    @property
    def issuer(self) -> str:
        return (self.settings.get_value(ISSUER_KEY) or "").rstrip("/")

    @property
    def client_id(self) -> str:
        return self.settings.get_value(CLIENT_ID_KEY) or ""

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id)

    def configure(self, issuer: str, client_id: str, *, claim: str = "") -> None:
        issuer = issuer.strip().rstrip("/")
        client_id = client_id.strip()
        if not issuer.startswith("https://") and not issuer.startswith("http://localhost"):
            raise ProviderError("issuer must be an https URL (http is allowed for localhost)")
        if not client_id:
            raise ProviderError("client_id must not be empty")
        self.settings.set_value(ISSUER_KEY, issuer)
        self.settings.set_value(CLIENT_ID_KEY, client_id)
        if claim.strip():
            self.settings.set_value(CLAIM_KEY, claim.strip())

    # --- login ------------------------------------------------------------
    def begin(self) -> DeviceCode:
        if not self.configured:
            raise NotAuthenticatedError(
                "no identity provider configured; run 'samurai auth configure <issuer> <client_id>'"
            )
        status, payload = self.transport(
            f"{self.issuer}/device_authorization",
            {
                "client_id": self.client_id,
                "scope": self.settings.get_value(SCOPES_KEY) or DEFAULT_SCOPES,
            },
            {},
        )
        if status != 200:
            raise ProviderError(f"device authorization failed: {status} {payload}")
        try:
            return DeviceCode(
                device_code=str(payload["device_code"]),
                user_code=str(payload["user_code"]),
                verification_uri=str(
                    payload.get("verification_uri") or payload["verification_url"]
                ),
                interval=int(payload.get("interval") or 5),
                expires_in=int(payload.get("expires_in") or 600),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"device authorization answer is unusable: {payload}") from exc

    def complete(self, code: DeviceCode, *, wait: bool = True) -> Session:
        """Poll for the token; `wait=False` gives one attempt for tests."""
        deadline = _now() + timedelta(seconds=code.expires_in)
        while True:
            status, payload = self.transport(
                f"{self.issuer}/token",
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": code.device_code,
                    "client_id": self.client_id,
                },
                {},
            )
            if status == 200:
                return self._establish(payload)
            if payload.get("error") != "authorization_pending":
                raise ProviderError(f"login failed: {payload.get('error') or status}")
            if not wait or _now() >= deadline:
                raise NotAuthenticatedError("the code was not confirmed in time")
            self.sleep(code.interval)

    def _establish(self, payload: dict[str, Any]) -> Session:
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise ProviderError("the provider returned no access token")
        claims = self._claims(access_token, payload.get("id_token"))
        subject = str(claims.get("sub") or "")
        if not subject:
            raise ProviderError("the token carries no subject")
        principal = Principal(
            subject=subject,
            email=str(claims.get("email") or ""),
            display=str(claims.get("name") or claims.get("preferred_username") or ""),
            role=self._role(claims),
            issuer=self.issuer,
        )
        expires_in = int(payload.get("expires_in") or 3600)
        self.access.end_open_sessions()
        self._store_token(ACCESS_SECRET, access_token)
        refresh = payload.get("refresh_token")
        if refresh:
            self._store_token(REFRESH_SECRET, str(refresh))
        session = Session(
            principal=principal, access_expires_at=_now() + timedelta(seconds=expires_in)
        )
        self.access.add_session(session)
        return session

    def _claims(self, access_token: str, id_token: object) -> dict[str, Any]:
        if isinstance(id_token, str) and id_token.count(".") == 2:
            return self._verified_claims(id_token)
        status, payload = self.transport(
            f"{self.issuer}/userinfo", None, {"Authorization": f"Bearer {access_token}"}
        )
        if status == 200 and isinstance(payload, dict):
            return payload
        raise ProviderError(f"cannot read the identity claims: {status}")

    def _verified_claims(self, id_token: str) -> dict[str, Any]:
        """Signature, issuer, audience and expiry — in that order of trust."""
        header = _jwt_part(id_token, 0)
        claims = _jwt_part(id_token, 1)
        algorithm = header.get("alg")
        kid = header.get("kid")
        if algorithm not in ("RS256", "ES256") or not isinstance(kid, str):
            raise ProviderError(f"unsupported id_token header: alg={algorithm} kid={kid}")
        key = self._key(kid)
        _verify(id_token, key, str(algorithm))
        if claims.get("iss") != self.issuer:
            raise ProviderError(f"id_token issuer {claims.get('iss')!r} is not {self.issuer!r}")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.client_id not in audiences:
            raise ProviderError("id_token was not issued for this client")
        expires = claims.get("exp")
        if not isinstance(expires, int) or expires + CLOCK_SKEW_SECONDS < int(time.time()):
            raise ProviderError("id_token has expired")
        return claims

    def _key(self, kid: str) -> Any:
        """The signing key for `kid`; an unknown kid reloads once for rotation."""
        keys = self._jwks_for(set())
        key = keys.get(kid)
        if key is None:
            keys = self._jwks_for(set(keys))
            key = keys.get(kid)
        if key is None:
            raise ProviderError(f"the provider publishes no key {kid!r}")
        return key

    def _jwks_for(self, known: set[str]) -> dict[str, Any]:
        fresh = self._jwks and time.monotonic() - self._jwks_at < JWKS_TTL_SECONDS
        if fresh and (not known or set(self._jwks) - known):
            return self._jwks
        status, payload = self.transport(f"{self.issuer}/jwks.json", None, {})
        if status != 200 or not isinstance(payload.get("keys"), list):
            raise ProviderError(f"cannot fetch the provider keys: {status}")
        keys: dict[str, Any] = {}
        for entry in payload["keys"]:
            if not isinstance(entry, dict):
                continue
            parsed = _public_key(entry)
            if parsed is not None:
                keys[str(entry.get("kid"))] = parsed
        if not keys:
            raise ProviderError("the provider published no usable key")
        self._jwks, self._jwks_at = keys, time.monotonic()
        return keys

    def _role(self, claims: dict[str, Any]) -> Role:
        claim_name = self.settings.get_value(CLAIM_KEY) or DEFAULT_CLAIM
        raw = claims.get(claim_name)
        values = raw if isinstance(raw, list) else [raw] if raw else []
        best: Role | None = None
        for value in values:
            mapped = self.access.get_mapping(str(value))
            if mapped is not None and (best is None or mapped.rank > best.rank):
                best = mapped
        # unmapped is the weakest role, never a convenient promotion
        return best or Role.VIEWER

    # --- session ----------------------------------------------------------
    def current(self) -> Principal:
        session = self.access.open_session()
        if session is None:
            if not self.configured:
                return Principal(subject="local", display="local", role=LOCAL_PRINCIPAL_ROLE)
            raise NotAuthenticatedError("not signed in; run 'samurai auth login'")
        if session.access_expires_at <= _now():
            session = self._refresh(session)
        return session.principal

    def _refresh(self, session: Session) -> Session:
        """Swap the tokens behind a session, or say the operator must sign in."""
        try:
            token = self.vault.get(REFRESH_SECRET)
        except Exception as exc:  # noqa: BLE001 - any vault failure means no token
            raise NotAuthenticatedError(
                "the session expired and has no refresh token; run 'samurai auth login'"
            ) from exc
        status, payload = self.transport(
            f"{self.issuer}/token",
            {"grant_type": "refresh_token", "refresh_token": token, "client_id": self.client_id},
            {},
        )
        if status != 200 or not payload.get("access_token"):
            # the provider revoked it; a stale session would only confuse the gate
            self.access.end_open_sessions()
            raise NotAuthenticatedError(
                "the provider no longer accepts the session; run 'samurai auth login'"
            )
        principal = session.principal
        id_token = payload.get("id_token")
        if isinstance(id_token, str) and id_token.count(".") == 2:
            claims = self._verified_claims(id_token)
            principal = principal.model_copy(
                update={
                    "email": str(claims.get("email") or principal.email),
                    "role": self._role(claims),
                }
            )
        self._store_token(ACCESS_SECRET, str(payload["access_token"]))
        if payload.get("refresh_token"):
            self._store_token(REFRESH_SECRET, str(payload["refresh_token"]))
        self.access.replace_principal(session.id, principal)
        expires_at = _now() + timedelta(seconds=int(payload.get("expires_in") or 3600))
        self.access.extend_session(session.id, expires_at)
        return session.model_copy(
            update={"principal": principal, "access_expires_at": expires_at}
        )

    def _store_token(self, name: str, value: str) -> None:
        if self.vault.repository.find_any(name) is not None:
            self.vault.remove(name)
        self.vault.add(name, value)

    def require(self, path: str) -> Principal:
        principal = self.current()
        require(principal, path)
        return principal

    def logout(self) -> bool:
        for name in (ACCESS_SECRET, REFRESH_SECRET):
            if self.vault.repository.find_any(name) is not None:
                self.vault.remove(name)
        return self.access.end_open_sessions() > 0

    def map_role(self, claim_value: str, role: Role) -> None:
        claim_value = claim_value.strip()
        if not claim_value:
            raise ProviderError("claim value must not be empty")
        self.access.set_mapping(claim_value, role)


def _jwt_part(token: str, index: int) -> dict[str, Any]:
    payload = token.split(".")[index]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        value: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded), strict=False)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProviderError(f"id_token part {index} is unreadable: {exc}") from exc
    return value


def _public_key(entry: dict[str, Any]) -> Any:
    """A JWK entry as a cryptography key, or None when the shape is unknown."""
    kind = entry.get("kty")
    try:
        if kind == "RSA":
            modulus = int.from_bytes(base64.urlsafe_b64decode(str(entry["n"]) + "=="), "big")
            exponent = int.from_bytes(base64.urlsafe_b64decode(str(entry["e"]) + "=="), "big")
            return rsa.RSAPublicNumbers(exponent, modulus).public_key()
        if kind == "EC":
            curve = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1()}.get(str(entry["crv"]))
            if curve is None:
                return None
            x = int.from_bytes(base64.urlsafe_b64decode(str(entry["x"]) + "=="), "big")
            y = int.from_bytes(base64.urlsafe_b64decode(str(entry["y"]) + "=="), "big")
            return ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
    except (KeyError, ValueError):
        return None
    return None


def _verify(token: str, key: Any, algorithm: str) -> None:
    signed, _, signature = token.rpartition(".")
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    try:
        if algorithm == "RS256":
            if not isinstance(key, rsa.RSAPublicKey):
                raise ProviderError("RS256 token signed with a non-RSA key")
            key.verify(raw, signed.encode(), padding.PKCS1v15(), hashes.SHA256())
        else:
            if not isinstance(key, ec.EllipticCurvePublicKey):
                raise ProviderError("ES256 token signed with a non-EC key")
            key.verify(raw, signed.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ProviderError("id_token signature does not verify") from exc


__all__ = ["ACCESS_SECRET", "AccessService", "CLIENT_ID_KEY", "ISSUER_KEY"]
