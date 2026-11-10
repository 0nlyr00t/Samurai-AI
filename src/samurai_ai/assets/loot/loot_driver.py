"""Pull credential-shaped strings out of one in-scope URL.

Runs inside the scoped sandbox: the fetch goes through the egress proxy it sets in
the environment, so the URL's host is judged by the same scope as any other tool.
Prints what it recognised first, then the (truncated) body for the artifact.
"""

import base64
import binascii
import re
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

USER_AGENT = "samurai-ai/loot"
MAX_BODY = 262144
MAX_HITS = 100

#: what a leaked secret looks like in a body or a header
PATTERNS = (
    ("basic", re.compile(r"Authorization:\s*Basic\s+([A-Za-z0-9+/=]{8,})", re.I)),
    ("url-cred", re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s:/@]+:[^\s@/]+)@", re.I)),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "pair",
        re.compile(
            r"\b(?:user(?:name)?|login|pass(?:word|wd)?|secret|token|api[_-]?key|"
            r"access[_-]?key)\b\s*[:=]\s*[\"']?([^\s\"'&<>;,]{3,80})",
            re.I,
        ),
    ),
    ("ftp", re.compile(r"^\s*(?:USER|PASS)\s+(\S{3,80})", re.M)),
)


def decode_basic(value: str) -> str:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", "replace")
    except (ValueError, binascii.Error):
        return value


def scan(text: str) -> list[str]:
    hits: list[str] = []
    seen: set[str] = set()
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            if label == "basic":
                value = decode_basic(value)
            value = value.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            where = text[max(0, match.start() - 40) : match.start()].replace("\n", " ")
            hits.append(f"{label}\t{value[:120]}\t{where.strip()[-40:]}")
            if len(hits) >= MAX_HITS:
                return hits
    return hits


def fetch(url: str) -> tuple[int, str, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as answer:  # noqa: S310 - fixed scheme set
        raw = answer.read(MAX_BODY)
        charset = answer.headers.get_content_charset() or "utf-8"
        try:
            body = raw.decode(charset, "replace")
        except LookupError:
            body = raw.decode("latin-1", "replace")
        return answer.status, str(answer.headers), body


def main(argv: list[str]) -> int:
    url = argv[1] if len(argv) > 1 else ""
    if not url:
        print("loot: no url given")
        return 1
    if urlsplit(url).scheme not in ("http", "https"):
        print(f"loot: only http(s) URLs: {url}")
        return 1
    try:
        status, headers, body = fetch(url)
    except Exception as exc:  # noqa: BLE001 - the message is the report
        print(f"loot: fetch failed: {exc}")
        return 1
    hits = scan(headers + "\n" + body)
    print(f"# loot {url} status={status} bytes={len(body)}")
    if not hits:
        print("no credential-shaped strings")
    for hit in hits:
        print(hit)
    print("--- body ---")
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
