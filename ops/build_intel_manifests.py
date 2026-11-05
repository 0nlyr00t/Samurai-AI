"""Generate the intel manifests with their driver scripts inlined.

A manifest cannot ship a file, and the sandbox has no mounts, so the driver has to
travel in argv as a `python3 -c` literal. The source stays in `assets/intel/` for
review; this script keeps the JSON in sync and a test fails when they drift.

    uv run python ops/build_intel_manifests.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEL = ROOT / "assets" / "intel"
TOOLS = ROOT / "assets" / "tools"

#: the interpreter image, pinned by *index* digest so arm64 runs natively
IMAGE = (
    "python:3.12-slim@sha256:"
    "b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"
)

TOOLS_SPEC = {
    "shodan": {
        "driver": "shodan_driver.py",
        "name": "intel.shodan",
        "title": "Shodan lookup",
        "description": "Passive Shodan data for one in-scope IP (services, banners, "
        "hostnames) or the DNS map of an in-scope domain",
        "modes": ("host", "domain"),
        "env_names": ("SHODAN_API_KEY",),
        "hosts": ("api.shodan.io",),
    },
    "censys": {
        "driver": "censys_driver.py",
        "name": "intel.censys",
        "title": "Censys lookup",
        "description": "Passive Censys host asset for an in-scope IP or a certificate "
        "search by an in-scope domain",
        "modes": ("host", "search"),
        "env_names": ("CENSYS_API_TOKEN", "CENSYS_ORG_ID"),
        "hosts": ("api.platform.censys.io",),
    },
}


def build(key: str, spec: dict[str, object]) -> dict[str, object]:
    script = (INTEL / str(spec["driver"])).read_text(encoding="utf-8")
    return {
        "id": spec["name"],
        "version": 1,
        "title": spec["title"],
        "description": spec["description"],
        "image": IMAGE,
        "executable": "python3",
        "risk_class": "r1",
        "network": "scoped",
        "read_only": True,
        # python has no proxy flag; urllib reads HTTP(S)_PROXY, which the runner sets
        "proxy_via_env": True,
        "egress_hosts": list(spec["hosts"]),
        "env_names": list(spec["env_names"]),
        "env": {"HOME": "/tmp", "TMPDIR": "/tmp"},
        "limits": {"timeout_seconds": 120, "memory_mb": 512, "max_output_bytes": 262144},
        "params": {
            "mode": {
                "type": "string",
                "enum": list(spec["modes"]),
                "default": spec["modes"][0],
                "description": " or ".join(spec["modes"]),
            },
            "target": {
                "type": "string",
                "required": True,
                "description": "in-scope IP (host) or in-scope domain (domain/search)",
            },
        },
        "targets": {"param": "target", "kind": "host", "max": 1},
        "argv": ["-c", script, "{mode}", "{target}"],
        "parser": "text-lines",
        "summary_limit": 100,
    }


def main() -> int:
    TOOLS.mkdir(parents=True, exist_ok=True)
    for key, spec in TOOLS_SPEC.items():
        target = TOOLS / f"{spec['name']}.json"
        target.write_text(json.dumps(build(key, spec), indent=2) + "\n", encoding="utf-8")
        print(target.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
