"""Generate the inline-driver manifests with the driver scripts built into argv.

A manifest cannot ship a file, and the sandbox mounts nothing, so a stdlib driver
has to travel in argv as a `python3 -c` literal. The sources stay reviewable in
`assets/intel/` and `assets/loot/`; this script keeps the JSON in sync and a test
fails when they drift.

    uv run python ops/build_inline_manifests.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "assets" / "tools"

#: the interpreter image, pinned by *index* digest so arm64 runs natively
IMAGE = (
    "python:3.12-slim@sha256:"
    "b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"
)

MODE_TARGET_PARAMS = {
    "mode": {
        "type": "string",
        "enum": ["host", "domain"],
        "default": "host",
        "description": "host or domain",
    },
    "target": {
        "type": "string",
        "required": True,
        "description": "in-scope IP (host) or in-scope domain (domain/search)",
    },
}
MODE_TARGET_ARGV = ["-c", "{script}", "{mode}", "{target}"]
MODE_TARGETS = {"param": "target", "kind": "host", "max": 1}

URL_PARAMS = {
    "url": {"type": "string", "required": True, "description": "in-scope URL"},
}
URL_ARGV = ["-c", "{script}", "{url}"]
URL_TARGETS = {"param": "url", "kind": "url-host", "max": 1}

TOOLS_SPEC = (
    {
        "driver": "assets/intel/shodan_driver.py",
        "name": "intel.shodan",
        "title": "Shodan lookup",
        "description": "Passive Shodan data for one in-scope IP (services, banners, "
        "hostnames) or the DNS map of an in-scope domain",
        "params": MODE_TARGET_PARAMS,
        "argv": MODE_TARGET_ARGV,
        "targets": MODE_TARGETS,
        "env_names": ("SHODAN_API_KEY",),
        "hosts": ("api.shodan.io",),
    },
    {
        "driver": "assets/intel/censys_driver.py",
        "name": "intel.censys",
        "title": "Censys lookup",
        "description": "Passive Censys host asset for an in-scope IP or a certificate "
        "search by an in-scope domain",
        "params": {
            "mode": {**MODE_TARGET_PARAMS["mode"], "enum": ["host", "search"]},
            "target": MODE_TARGET_PARAMS["target"],
        },
        "argv": MODE_TARGET_ARGV,
        "targets": MODE_TARGETS,
        "env_names": ("CENSYS_API_TOKEN", "CENSYS_ORG_ID"),
        "hosts": ("api.platform.censys.io",),
    },
    {
        "driver": "assets/loot/loot_driver.py",
        "name": "loot.scan",
        "title": "Credential scan of a body",
        "description": "Fetch one in-scope URL and pull credential-shaped strings out "
        "of it (basic auth, URL credentials, tokens, key material)",
        "params": URL_PARAMS,
        "argv": URL_ARGV,
        "targets": URL_TARGETS,
        "env_names": (),
        "hosts": (),
        "risk": "r1",
        "timeout": 180,
    },
)


def build(spec: dict[str, object]) -> dict[str, object]:
    script = (ROOT / str(spec["driver"])).read_text(encoding="utf-8")
    argv = [script if part == "{script}" else part for part in spec["argv"]]  # type: ignore[union-attr]
    return {
        "id": spec["name"],
        "version": 1,
        "title": spec["title"],
        "description": spec["description"],
        "image": IMAGE,
        "executable": "python3",
        "risk_class": spec.get("risk", "r1"),
        "network": "scoped",
        "read_only": True,
        # python has no proxy flag; urllib reads HTTP(S)_PROXY, which the runner sets
        "proxy_via_env": True,
        "egress_hosts": list(spec["hosts"]),  # type: ignore[call-overload]
        "env_names": list(spec["env_names"]),  # type: ignore[call-overload]
        "env": {"HOME": "/tmp", "TMPDIR": "/tmp"},
        "limits": {
            "timeout_seconds": spec.get("timeout", 120),
            "memory_mb": 512,
            "max_output_bytes": 524288 if spec["name"] == "loot.scan" else 262144,
        },
        "params": spec["params"],
        "targets": spec["targets"],
        "argv": argv,
        "parser": "text-lines",
        "summary_limit": 100,
    }


def main() -> int:
    TOOLS.mkdir(parents=True, exist_ok=True)
    for spec in TOOLS_SPEC:
        target = TOOLS / f"{spec['name']}.json"
        target.write_text(json.dumps(build(spec), indent=2) + "\n", encoding="utf-8")
        print(target.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
