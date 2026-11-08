"""Device-code (RFC 8628) login for model providers that offer OAuth.

Vendors differ in endpoints, so the issuer and client id are settings, not code:
`model.oauth.<provider>.issuer` and `...client_id`. The token lands in the vault
as `<provider>-access-token` and `_build_provider` prefers it over an API key.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from samurai_ai.domain.models import ModelError

#: transport(url, form) → (status, json)
Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, object]]]


@dataclass(frozen=True)
class DevicePrompt:
    """What the operator has to do: open the URL and type the code."""

    verification_url: str
    user_code: str
    device_code: str
    interval: float


class DeviceFlowError(ModelError):
    pass


def begin(transport: Transport, issuer: str, client_id: str, scope: str = "") -> DevicePrompt:
    form = {"client_id": client_id}
    if scope:
        form["scope"] = scope
    status, payload = transport(f"{issuer.rstrip('/')}/device_authorization", form)
    if status >= 400:
        raise DeviceFlowError(f"device authorization failed with HTTP {status}")
    url = str(payload.get("verification_uri") or payload.get("verification_url") or "")
    code = str(payload.get("user_code") or "")
    device = str(payload.get("device_code") or "")
    if not (url and code and device):
        raise DeviceFlowError("provider did not return a device code")
    raw_interval = payload.get("interval")
    interval = float(raw_interval) if isinstance(raw_interval, (int, float)) else 5.0
    return DevicePrompt(url, code, device, interval)


def wait_for_token(
    transport: Transport,
    issuer: str,
    client_id: str,
    prompt: DevicePrompt,
    *,
    timeout: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Poll until the operator finishes in the browser; returns the token."""
    deadline = time.monotonic() + timeout
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": prompt.device_code,
        "client_id": client_id,
    }
    while time.monotonic() < deadline:
        sleep(prompt.interval)
        status, payload = transport(f"{issuer.rstrip('/')}/token", form)
        token = payload.get("access_token")
        if isinstance(token, str) and token:
            return token
        error = str(payload.get("error") or "")
        if status >= 400 and error not in ("authorization_pending", "slow_down"):
            raise DeviceFlowError(f"device login refused: {error or status}")
    raise DeviceFlowError("device login timed out")
