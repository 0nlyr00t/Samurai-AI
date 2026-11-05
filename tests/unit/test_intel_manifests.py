"""The intel manifests: generated from the drivers, env-fed, proxy by env only."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from samurai_ai.domain.tool_manifest import ToolManifest
from samurai_ai.execution.manifests import render_argv

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "assets" / "tools"
INTEL = ROOT / "assets" / "intel"
NAMES = ("intel.shodan", "intel.censys")


def manifest(name: str) -> ToolManifest:
    raw = json.loads((TOOLS / f"{name}.json").read_text(encoding="utf-8"))
    return ToolManifest.model_validate({**raw, "trusted": True})


def test_the_generated_files_match_their_sources() -> None:
    """Regenerating must be a no-op, or the reviewed driver is not what runs."""
    before = {name: (TOOLS / f"{name}.json").read_text(encoding="utf-8") for name in NAMES}
    subprocess.run(
        [sys.executable, str(ROOT / "ops" / "build_intel_manifests.py")],
        cwd=ROOT, check=True, capture_output=True,
    )
    for name in NAMES:
        assert (TOOLS / f"{name}.json").read_text(encoding="utf-8") == before[name]


@pytest.mark.parametrize("name", NAMES)
def test_the_manifest_is_read_only_r1_and_proxy_fed_by_env(name: str) -> None:
    parsed = manifest(name)
    assert parsed.risk_class == "r1" and parsed.read_only
    assert parsed.network == "scoped"
    # python has no proxy flag: the environment is the only channel
    assert parsed.proxy_via_env and parsed.proxy_flag is None
    assert parsed.profile().proxy_via_env


@pytest.mark.parametrize("name", NAMES)
def test_the_key_is_declared_as_env_not_as_a_parameter(name: str) -> None:
    parsed = manifest(name)
    assert parsed.env_names
    assert all(not env.endswith("KEY") or env.isupper() for env in parsed.env_names)
    assert set(parsed.params) == {"mode", "target"}


@pytest.mark.parametrize("name", NAMES)
def test_the_driver_travels_in_argv_and_braces_survive(name: str) -> None:
    parsed = manifest(name)
    argv = render_argv(parsed, {"mode": "host", "target": "10.0.0.5"})
    script = argv[argv.index("-c") + 1]
    source = (INTEL / ("shodan_driver.py" if "shodan" in name else "censys_driver.py"))
    assert script == source.read_text(encoding="utf-8")
    # f-string braces inside the script stay put, placeholders around it resolve
    assert "{" in script
    assert argv[-2:] == ["host", "10.0.0.5"]


def test_a_driver_without_a_key_says_so(tmp_path: Path) -> None:
    script = INTEL / "shodan_driver.py"
    done = subprocess.run(
        [sys.executable, str(script), "host", "10.0.0.5"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert done.returncode == 1
    assert "SHODAN_API_KEY is empty" in done.stdout
