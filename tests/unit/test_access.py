"""Roles and the device login: who may run what, and how identity arrives."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from samurai_ai.domain.access import (
    ForbiddenError,
    NotAuthenticatedError,
    Principal,
    ProviderError,
    Role,
    minimum_role,
    require,
)
from samurai_ai.services.access import ACCESS_SECRET, AccessService
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.access import AccessRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.vault import VaultRepository


def _jwt(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AccessService:
    monkeypatch.delenv("SAMURAI_VAULT_KEY", raising=False)
    database = Database(tmp_path / "access.db")
    vault = VaultService(
        VaultRepository(database), OperationRepository(database), tmp_path / "vault.key"
    )
    answers: list[tuple[int, dict[str, Any]]] = []

    def transport(url: str, body: dict[str, Any] | None, headers: dict[str, str]):
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


def test_a_mapped_claim_becomes_the_role(service: AccessService) -> None:
    service.configure("https://idp.test", "client")
    service.map_role("secops", Role.OPERATOR)
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (200, {"access_token": "at", "id_token": _jwt({"sub": "u1", "groups": ["secops"]})}),
        ]
    )

    session = service.complete(service.begin())

    assert session.principal.role is Role.OPERATOR
    assert session.principal.subject == "u1"


def test_an_unmapped_claim_is_the_weakest_role(service: AccessService) -> None:
    service.configure("https://idp.test", "client")
    service.map_role("secops", Role.OPERATOR)
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (200, {"access_token": "at", "id_token": _jwt({"sub": "u2", "groups": ["other"]})}),
        ]
    )

    session = service.complete(service.begin())

    assert session.principal.role is Role.VIEWER


def test_the_strongest_mapped_group_wins(service: AccessService) -> None:
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
                    "id_token": _jwt({"sub": "u3", "groups": ["readers", "admins"]}),
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


def test_the_token_lands_in_the_vault_not_the_row(service: AccessService) -> None:
    service.configure("https://idp.test", "client")
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (200, {"access_token": "secret-token", "id_token": _jwt({"sub": "u4"})}),
        ]
    )
    service.complete(service.begin())

    assert service.vault.get(ACCESS_SECRET) == "secret-token"
    created = service.access.open_session()
    assert created is not None
    assert "secret-token" not in json.dumps(created.model_dump(mode="json"))


def test_logout_ends_the_session(service: AccessService) -> None:
    service.configure("https://idp.test", "client")
    service.answers.extend(  # type: ignore[attr-defined]
        [
            (200, {"user_code": "ABCD", "device_code": "dev", "verification_uri": "https://x"}),
            (200, {"access_token": "at", "id_token": _jwt({"sub": "u5"})}),
        ]
    )
    service.complete(service.begin())

    assert service.logout() is True
    with pytest.raises(NotAuthenticatedError):
        service.current()


def test_a_new_login_closes_the_previous_session(service: AccessService) -> None:
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
                (200, {"access_token": "at", "id_token": _jwt({"sub": subject})}),
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
