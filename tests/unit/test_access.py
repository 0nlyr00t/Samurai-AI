"""Roles and the device login: who may run what, and how identity arrives."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from samurai_ai.domain.access import (
    ForbiddenError,
    NotAuthenticatedError,
    Principal,
    ProviderError,
    Role,
    minimum_role,
    require,
)
from samurai_ai.services.access import ACCESS_SECRET, REFRESH_SECRET, AccessService
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.access import AccessRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.vault import VaultRepository


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Idp:
    """A minimal provider: one RSA key, signed tokens, a JWKS document."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-1"
        self.issuer = "https://idp.test"

    def jwks(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "n": _b64(numbers.n.to_bytes(256, "big")),
                    "e": _b64(numbers.e.to_bytes(3, "big")),
                }
            ]
        }

    def sign(self, claims: dict[str, Any], *, kid: str | None = None) -> str:
        claims = {
            "iss": self.issuer,
            "aud": "client",
            "exp": int(time.time()) + 3600,
            **claims,
        }
        header = {"alg": "RS256", "kid": kid or self.kid}
        signed = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
        signature = self.key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signed}.{_b64(signature)}"


@pytest.fixture
def idp() -> Idp:
    return Idp()


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: Idp) -> AccessService:
    monkeypatch.delenv("SAMURAI_VAULT_KEY", raising=False)
    database = Database(tmp_path / "access.db")
    vault = VaultService(
        VaultRepository(database), OperationRepository(database), tmp_path / "vault.key"
    )
    answers: list[tuple[int, dict[str, Any]]] = []

    def transport(url: str, body: dict[str, Any] | None, headers: dict[str, str]):
        if url.endswith("/jwks.json"):
            return 200, idp.jwks()
        return answers.pop(0)

    access = AccessService(
        SettingsService(RecoveryRepository(database)),
        vault,
        AccessRepository(database),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    access.answers = answers  # type: ignore[attr-defined]
    return access


def test_an_unconfigured_install_runs_as_a_local_admin(service: AccessService) -> None:
    principal = service.current()

    assert principal.role is Role.ADMIN
    assert principal.subject == "local"


def test_a_configured_install_requires_a_login(service: AccessService) -> None:
    service.configure("https://idp.test", "client")

    with pytest.raises(NotAuthenticatedError):
        service.current()


def test_issuer_must_be_https(service: AccessService) -> None:
    with pytest.raises(ProviderError):
        service.configure("http://idp.test", "client")
    service.configure("http://localhost:8080", "client")
    assert service.configured


def test_a_mapped_claim_becomes_the_role(service: AccessService, idp: Idp) -> None:
    service.configure("https://idp.test", "client")
    service.map_role("secops", Role.OPERATOR)
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (
                200,
                {"access_token": "at", "id_token": idp.sign({"sub": "u1", "groups": ["secops"]})},
            ),
        ]
    )

    session = service.complete(service.begin())

    assert session.principal.role is Role.OPERATOR
    assert session.principal.subject == "u1"


def test_an_unmapped_claim_is_the_weakest_role(service: AccessService, idp: Idp) -> None:
    service.configure("https://idp.test", "client")
    service.map_role("secops", Role.OPERATOR)
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (
                200,
                {"access_token": "at", "id_token": idp.sign({"sub": "u2", "groups": ["other"]})},
            ),
        ]
    )

    session = service.complete(service.begin())

    assert session.principal.role is Role.VIEWER


def test_the_strongest_mapped_group_wins(service: AccessService, idp: Idp) -> None:
    service.configure("https://idp.test", "client")
    service.map_role("readers", Role.VIEWER)
    service.map_role("admins", Role.ADMIN)
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (
                200,
                {
                    "access_token": "at",
                    "id_token": idp.sign({"sub": "u3", "groups": ["readers", "admins"]}),
                },
            ),
        ]
    )

    assert service.complete(service.begin()).principal.role is Role.ADMIN


def test_an_unconfirmed_code_is_not_a_login(service: AccessService) -> None:
    service.configure("https://idp.test", "client")
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (400, {"error": "authorization_pending"}),
        ]
    )

    with pytest.raises(NotAuthenticatedError):
        service.complete(service.begin(), wait=False)


def test_a_rejected_code_names_the_reason(service: AccessService) -> None:
    service.configure("https://idp.test", "client")
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (400, {"error": "access_denied"}),
        ]
    )

    with pytest.raises(ProviderError, match="access_denied"):
        service.complete(service.begin())


def test_the_token_lands_in_the_vault_not_the_row(service: AccessService, idp: Idp) -> None:
    service.configure("https://idp.test", "client")
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (200, {"access_token": "secret-token", "id_token": idp.sign({"sub": "u4"})}),
        ]
    )
    service.complete(service.begin())

    assert service.vault.get(ACCESS_SECRET) == "secret-token"
    created = service.access.open_session()
    assert created is not None
    assert "secret-token" not in json.dumps(created.model_dump(mode="json"))


def test_logout_ends_the_session(service: AccessService, idp: Idp) -> None:
    service.configure("https://idp.test", "client")
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (200, {"access_token": "at", "id_token": idp.sign({"sub": "u5"})}),
        ]
    )
    service.complete(service.begin())

    assert service.logout() is True
    with pytest.raises(NotAuthenticatedError):
        service.current()


def test_a_new_login_closes_the_previous_session(service: AccessService, idp: Idp) -> None:
    service.configure("https://idp.test", "client")
    for subject in ("u6", "u7"):
        service.answers.extend(  # type: ignore[attr-defined]
            [
                (
                    200,
                    {
                        "user_code": "ABCD",
                        "device_code": "dev",
                        "verification_uri": "https://x",
                    },
                ),
                (200, {"access_token": "at", "id_token": idp.sign({"sub": subject})}),
            ]
        )
        service.complete(service.begin())

    sessions = service.access.list_sessions()
    assert sum(1 for session in sessions if session.open) == 1


# --- the gate -------------------------------------------------------------
def test_unknown_commands_need_an_operator() -> None:
    assert minimum_role("future.command") is Role.OPERATOR
    assert minimum_role("future") is Role.OPERATOR


def test_reading_needs_only_a_viewer() -> None:
    assert minimum_role("operation.show") is Role.VIEWER
    assert minimum_role("role.list") is Role.VIEWER


def test_secrets_need_an_admin() -> None:
    assert minimum_role("secret.add") is Role.ADMIN


def test_a_viewer_is_refused_an_operator_command() -> None:
    viewer = Principal(subject="v", role=Role.VIEWER)

    with pytest.raises(ForbiddenError, match="needs the admin role"):
        require(viewer, "operation.create")


def test_a_viewer_is_refused_an_operator_command_too() -> None:
    viewer = Principal(subject="v", role=Role.VIEWER)

    with pytest.raises(ForbiddenError, match="needs the operator role"):
        require(viewer, "task.start")


def test_a_viewer_may_read() -> None:
    require(Principal(subject="v", role=Role.VIEWER), "operation.show")


def test_an_admin_may_do_anything() -> None:
    require(Principal(subject="a", role=Role.ADMIN), "secret.add")


# --- signature, issuer, audience, expiry -----------------------------------
def _login(service: AccessService, idp: Idp, token: str) -> None:
    service.configure(idp.issuer, "client")
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (200, {"access_token": "at", "id_token": token}),
        ]
    )


def test_a_forged_signature_is_refused(service: AccessService, idp: Idp) -> None:
    token = idp.sign({"sub": "u1"})
    head, body, _signature = token.split(".")
    _login(service, idp, f"{head}.{body}.{_b64(b'forged-signature-here-padding')}")

    with pytest.raises(ProviderError, match="signature does not verify"):
        service.complete(service.begin())


def test_the_wrong_issuer_is_refused(service: AccessService, idp: Idp) -> None:
    _login(service, idp, idp.sign({"sub": "u1", "iss": "https://evil.test"}))

    with pytest.raises(ProviderError, match="issuer"):
        service.complete(service.begin())


def test_another_clients_token_is_refused(service: AccessService, idp: Idp) -> None:
    _login(service, idp, idp.sign({"sub": "u1", "aud": "someone-else"}))

    with pytest.raises(ProviderError, match="not issued for this client"):
        service.complete(service.begin())


def test_an_expired_token_is_refused(service: AccessService, idp: Idp) -> None:
    _login(service, idp, idp.sign({"sub": "u1", "exp": int(time.time()) - 120}))

    with pytest.raises(ProviderError, match="expired"):
        service.complete(service.begin())


def test_a_small_clock_difference_is_tolerated(service: AccessService, idp: Idp) -> None:
    _login(service, idp, idp.sign({"sub": "u1", "exp": int(time.time()) - 10}))

    assert service.complete(service.begin()).principal.subject == "u1"


def test_an_unknown_key_is_fetched_again(service: AccessService, idp: Idp) -> None:
    service.configure(idp.issuer, "client")
    service.complete(_prime(service, idp))  # fills the cache with kid-1
    idp.kid = "kid-2"
    _login(service, idp, idp.sign({"sub": "u9"}))

    assert service.complete(service.begin()).principal.subject == "u9"


def _prime(service: AccessService, idp: Idp):  # type: ignore[no-untyped-def]
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "AB", "device_code": "d1", "verification_uri": "https://x"}),
            (200, {"access_token": "at", "id_token": idp.sign({"sub": "warm"})}),
        ]
    )
    return service.begin()


def test_an_unsupported_algorithm_is_refused(service: AccessService, idp: Idp) -> None:
    token = idp.sign({"sub": "u1"})
    head, body, signature = token.split(".")
    header = _b64(json.dumps({"alg": "none", "kid": idp.kid}).encode())
    _login(service, idp, f"{header}.{body}.{signature}")

    with pytest.raises(ProviderError, match="unsupported id_token header"):
        service.complete(service.begin())


# --- refresh --------------------------------------------------------------
def _signed_in(service: AccessService, idp: Idp) -> None:
    service.configure(idp.issuer, "client")
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (
                200,
                {
                    "access_token": "at",
                    "id_token": idp.sign({"sub": "u1"}),
                    "refresh_token": "rt",
                },
            ),
        ]
    )
    service.complete(service.begin())


def test_an_expired_session_refreshes_itself(service: AccessService, idp: Idp) -> None:
    _signed_in(service, idp)
    session = service.access.open_session()
    assert session is not None
    service.access.extend_session(session.id, session.access_expires_at.replace(year=2000))
    service.answers.append(  # type: ignore[attr-defined]
        (200, {"access_token": "at2", "expires_in": 600, "id_token": idp.sign({"sub": "u1"})})
    )

    assert service.current().subject == "u1"
    assert service.vault.get(ACCESS_SECRET) == "at2"


def test_refresh_keeps_the_role_from_the_new_claim(service: AccessService, idp: Idp) -> None:
    service.map_role("admins", Role.ADMIN)
    _signed_in(service, idp)
    session = service.access.open_session()
    assert session is not None
    service.access.extend_session(session.id, session.access_expires_at.replace(year=2000))
    service.answers.append(  # type: ignore[attr-defined]
        (
            200,
            {
                "access_token": "at2",
                "id_token": idp.sign({"sub": "u1", "groups": ["admins"]}),
            },
        )
    )

    assert service.current().role is Role.ADMIN


def test_a_revoked_refresh_forces_a_new_login(service: AccessService, idp: Idp) -> None:
    _signed_in(service, idp)
    session = service.access.open_session()
    assert session is not None
    service.access.extend_session(session.id, session.access_expires_at.replace(year=2000))
    service.answers.append((400, {"error": "invalid_grant"}))  # type: ignore[attr-defined]

    with pytest.raises(NotAuthenticatedError, match="no longer accepts"):
        service.current()
    assert service.access.open_session() is None


def test_logout_forgets_the_refresh_token(service: AccessService, idp: Idp) -> None:
    _signed_in(service, idp)

    service.logout()

    assert service.vault.repository.find_any(REFRESH_SECRET) is None
