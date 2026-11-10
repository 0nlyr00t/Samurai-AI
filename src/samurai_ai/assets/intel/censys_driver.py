"""Censys Platform lookups: one bare IP (host asset) or a certificate search.

Auth is a Personal Access Token in a Bearer header; the organization id is
optional and only sent when CENSYS_ORG_ID is set.
"""

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.platform.censys.io/v3/global"
USER_AGENT = "samurai-ai/intel"
ACCEPT_HOST = "application/vnd.censys.api.v3.host.v1+json"


def fail(message: str) -> None:
    print(f"censys: {message}")
    sys.exit(1)


def request(path: str, accept: str, **params: object) -> dict[str, Any]:
    token = os.environ.get("CENSYS_API_TOKEN", "")
    if not token:
        fail("CENSYS_API_TOKEN is empty — set it in /settings")
    org = os.environ.get("CENSYS_ORG_ID", "")
    if org:
        params["organization_id"] = org
    url = f"{BASE}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    # Cloudflare in front of the API rejects the default urllib agent
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "User-Agent": USER_AGENT,
    }
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as answer:  # noqa: S310
            payload: dict[str, Any] = json.loads(answer.read().decode())
            return payload
    except HTTPError as exc:
        fail(f"HTTP {exc.code}: {exc.read().decode()[:200]}")
    except URLError as exc:
        fail(f"unreachable: {exc.reason}")
    return {}


def host(ip: str) -> None:
    payload = request(f"/asset/host/{ip}", ACCEPT_HOST)
    host_data = (payload.get("result") or {}).get("resource") or {}
    location = host_data.get("location") or {}
    system = host_data.get("autonomous_system") or {}
    print(
        f"{ip}\t{location.get('country') or '-'}\t{system.get('asn') or '-'}"
        f"\t{(system.get('description') or '-')[:40]}"
    )
    names = ((host_data.get("dns") or {}).get("names")) or []
    if names:
        print("dns\t" + ",".join(str(name) for name in names[:25]))
    for service in (host_data.get("services") or [])[:50]:
        banner = str(service.get("banner") or "").strip().splitlines()
        first = banner[0][:120] if banner else ""
        print(
            f"service\t{service.get('port')}/{service.get('transport_protocol', 'tcp')}"
            f"\t{(service.get('protocol') or '')[:20]}\t{first}"
        )


def search(name: str) -> None:
    payload = request(
        "/search/query",
        "application/json",
        q=f"host.name: {name}",
        per_page=25,
    )
    for hit in ((payload.get("result") or {}).get("hits")) or []:
        resource = hit.get("host") or hit
        print(f"hit\t{resource.get('ip') or '-'}\t{(resource.get('name') or '')[:60]}")


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: censys_driver.py <host|search> <value>")
    mode, value = sys.argv[1], sys.argv[2]
    if mode == "host":
        host(value)
    elif mode == "search":
        search(value)
    else:
        fail(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
