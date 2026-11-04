"""Optional placeholders: a missing key takes its flag with it, never a dangling one."""

import json

import pytest
from pydantic import ValidationError

from samurai_ai.domain.tool_manifest import ToolManifest
from samurai_ai.execution.manifests import render_argv

BASE = {
    "id": "demo.scan",
    "title": "demo",
    "image": "ghcr.io/x/y@sha256:" + "a" * 64,
    "executable": "scan",
    "network": "none",
    "argv": ["--url", "{url}", "--api-token", "{token}", "--enumerate", "{what}"],
    "params": {
        "url": {"type": "string", "required": True},
        "token": {"type": "string", "optional": True, "default": "{{secret:demo_key}}"},
        "what": {"type": "string", "optional": True, "default": "vp"},
    },
}


def manifest(**overrides) -> ToolManifest:
    return ToolManifest.model_validate({**BASE, **overrides, "trusted": True})


def test_a_present_value_renders_normally() -> None:
    argv = render_argv(manifest(), {"url": "http://x/", "token": "abc", "what": "u"})
    assert argv == ["--url", "http://x/", "--api-token", "abc", "--enumerate", "u"]


def test_a_missing_optional_drops_the_flag_too() -> None:
    argv = render_argv(manifest(), {"url": "http://x/", "what": None, "token": None})
    assert argv == ["--url", "http://x/"]


def test_only_the_optional_one_is_dropped() -> None:
    argv = render_argv(manifest(), {"url": "http://x/", "token": None, "what": "vp"})
    assert argv == ["--url", "http://x/", "--enumerate", "vp"]


def test_optional_needs_a_default_and_is_string_only() -> None:
    with pytest.raises(ValidationError, match="optional"):
        manifest(params={"url": {"type": "string", "required": True},
                         "token": {"type": "string", "optional": True},
                         "what": {"type": "string"}})
    with pytest.raises(ValidationError):
        manifest(params={"url": {"type": "string", "required": True},
                         "token": {"type": "integer", "optional": True}})


def test_plain_env_is_validated_and_carried_into_the_profile() -> None:
    profile = manifest(env={"HOME": "/tmp", "XDG_CACHE_HOME": "/tmp/.cache"}).profile()
    assert profile.env == {"HOME": "/tmp", "XDG_CACHE_HOME": "/tmp/.cache"}
    with pytest.raises(ValidationError, match="upper case"):
        manifest(env={"home": "/tmp"})
    with pytest.raises(ValidationError, match="single line"):
        manifest(env={"HOME": "/tmp\n/tmp2"})


def test_a_double_dash_proxy_flag_is_allowed() -> None:
    m = manifest(network="scoped", proxy_flag="--proxy")
    assert m.proxy_flag == "--proxy"
    assert m.profile().proxy_flag == "--proxy"
    assert json.loads(m.model_dump_json())["proxy_flag"] == "--proxy"
