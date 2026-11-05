"""Shodan lookups for the harness: one bare IP (host) or a domain (dns).

The API is a GET returning JSON; the CLI would only add pip to the image. The key
travels in the query string because that is Shodan's shape — it is still out of
the tool's own argv.
"""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.shodan.io"
USER_AGENT = "samurai-ai/intel"
MAX_HOSTS = 25


def fail(message: str) -> None:
    print(f"shodan: {message}")
    sys.exit(1)


def get(path: str, **params) -> dict:
    key = os.environ.get("SHODAN_API_KEY", "")
    if not key:
        fail("SHODAN_API_KEY is empty — set it in /settings")
    params["key"] = key
    url = f"{BASE}{path}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=60) as answer:  # noqa: S310 - fixed host
            return json.loads(answer.read().decode())
    except HTTPError as exc:
        fail(f"HTTP {exc.code}: {exc.read().decode()[:200]}")
    except URLError as exc:
        fail(f"unreachable: {exc.reason}")
    return {}


def host(ip: str) -> None:
    data = get(f"/shodan/host/{ip}")
    print(f"{ip}\t{data.get('org') or '-'}\t{data.get('isp') or '-'}\t{data.get('os') or '-'}")
    ports = data.get("ports") or []
    if ports:
        print("ports\t" + ",".join(str(port) for port in ports[:100]))
    for service in (data.get("data") or [])[:50]:
        banner = (service.get("data") or "").strip().splitlines()
        first = banner[0][:120] if banner else ""
        print(
            f"service\t{service.get('port')}/{service.get('transport', 'tcp')}"
            f"\t{(service.get('product') or '')[:40]}\t{first}"
        )
    hosts = data.get("hostnames") or []
    if hosts:
        print("hostnames\t" + ",".join(hosts[:MAX_HOSTS]))


def domain(name: str) -> None:
    data = get(f"/dns/domain/{name}")
    for sub in (data.get("subdomains") or [])[:MAX_HOSTS * 4]:
        print(f"subdomain\t{sub}.{name}")
    for record in (data.get("data") or [])[:200]:
        kind = record.get("type")
        where = record.get("subdomain") or ""
        print(f"record\t{kind}\t{where}\t{record.get('value') or ''}")


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: shodan_driver.py <host|domain> <value>")
    mode, value = sys.argv[1], sys.argv[2]
    if mode == "host":
        host(value)
    elif mode == "domain":
        domain(value)
    else:
        fail(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
