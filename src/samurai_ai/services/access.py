"""OIDC device login and the role gate.

The gate is one call on the command path. Identity comes from the provider and
the role from its claim; when no provider is configured the install behaves as a
local admin, so upgrading does not lock anyone out mid-campaign.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

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
        if self.vault.repository.find_any(ACCESS_SECRET) is not None:
            self.vault.remove(ACCESS_SECRET)
        self.vault.add(ACCESS_SECRET, access_token)
        refresh = payload.get("refresh_token")
        if refresh:
            if self.vault.repository.find_any(REFRESH_SECRET) is not None:
                self.vault.remove(REFRESH_SECRET)
            self.vault.add(REFRESH_SECRET, str(refresh))
        session = Session(
            principal=principal, access_expires_at=_now() + timedelta(seconds=expires_in)
        )
        self.access.add_session(session)
        return session

    def _claims(self, access_token: str, id_token: object) -> dict[str, Any]:
        if isinstance(id_token, str) and id_token.count(".") == 2:
            return _jwt_claims(id_token)
        status, payload = self.transport(
            f"{self.issuer}/userinfo", None, {"Authorization": f"Bearer {access_token}"}
        )
        if status == 200 and isinstance(payload, dict):
            return payload
        raise ProviderError(f"cannot read the identity claims: {status}")

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
        if session is not None and session.access_expires_at > _now():
            return session.principal
        if session is not None:
            raise NotAuthenticatedError("the session expired; run 'samurai auth login'")
        if not self.configured:
            return Principal(subject="local", display="local", role=LOCAL_PRINCIPAL_ROLE)
        raise NotAuthenticatedError("not signed in; run 'samurai auth login'")

    def require(self, path: str) -> Principal:
        principal = self.current()
        require(principal, path)
        return principal

    def logout(self) -> bool:
        return self.access.end_open_sessions() > 0

    def map_role(self, claim_value: str, role: Role) -> None:
        claim_value = claim_value.strip()
        if not claim_value:
            raise ProviderError("claim value must not be empty")
        self.access.set_mapping(claim_value, role)


def _jwt_claims(token: str) -> dict[str, Any]:
    """Read claims without verifying — the token came over TLS from the issuer."""
    import base64

    payload = token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
        return claims
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProviderError(f"id_token claims are unreadable: {exc}") from exc
