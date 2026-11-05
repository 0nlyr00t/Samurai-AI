"""Manifest validation and rendering: the argv boundary."""

import pytest

from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.tool_manifest import ManifestError, ToolManifest
from samurai_ai.execution.manifests import ManifestTool, render_argv, render_stdin

BASE = {
    "id": "scan.demo",
    "title": "Demo",
    "image": "busybox@sha256:" + "a" * 64,
    "executable": "sh",
    "risk_class": "r2",
    "network": "none",
    "params": {
        "target": {"type": "string", "required": True, "pattern": "^[a-z.]+$"},
        "flags": {"type": "array", "items_enum": ["-x", "fast"], "default": ["fast"]},
    },
    "targets": {"param": "target", "kind": "host", "max": 1},
    "argv": ["-n", "{target}", "{flags}"],
    "parser": "text-lines",
}


def manifest(**overrides: object) -> ToolManifest:
    return ToolManifest.model_validate({**BASE, **overrides})


def test_a_valid_manifest_renders_typed_values() -> None:
    tool = manifest()
    argv = render_argv(tool, {"target": "example.com", "flags": ["fast", "-x"]})
    assert argv == ["-n", "example.com", "fast,-x"]


def test_a_value_cannot_smuggle_a_flag_or_a_space() -> None:
    tool = manifest(
        params={
            "target": {"type": "string", "required": True},
            "flags": {"type": "array", "default": ["fast"]},
        }
    )
    with pytest.raises(ManifestError, match="spaces or NUL"):
        render_argv(tool, {"target": "example.com --script"})
    with pytest.raises(ManifestError, match="must not start with"):
        render_argv(tool, {"target": "-e"})
    with pytest.raises(ManifestError, match="spaces or NUL"):
        render_argv(tool, {"target": "a\x00b"})


def test_enum_and_pattern_are_enforced_before_rendering() -> None:
    tool = manifest()
    with pytest.raises(ManifestError, match="does not match its pattern"):
        render_argv(tool, {"target": "EXAMPLE"})
    with pytest.raises(ManifestError, match="must be one of"):
        render_argv(tool, {"target": "example.com", "flags": ["-x", "slow"]})


def test_a_default_is_used_when_the_parameter_is_absent() -> None:
    assert render_argv(manifest(), {"target": "example.com"}) == [
        "-n",
        "example.com",
        "fast",
    ]


def test_stdin_rendering_joins_lines() -> None:
    tool = manifest(stdin_from="flags")
    assert render_stdin(tool, {"flags": ["a", "b"]}) == b"a\nb\n"
    assert render_stdin(tool, {}) is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"image": "busybox:latest"}, "pinned by digest"),
        ({"id": "Demo"}, "dotted kebab-case"),
        ({"parser": "yaml"}, "parser"),
        ({"argv": ["{missing}"]}, "unknown parameters"),
        ({"risk_class": "r9"}, "risk_class"),
        (
            {
                "params": {
                    "target": {"type": "integer", "required": True},
                    "flags": {"type": "array", "default": ["fast"]},
                }
            },
            "string or an array",
        ),
        ({"proxy_flag": "proxy"}, "proxy_flag"),
    ],
)
def test_broken_manifests_are_refused(overrides: dict, message: str) -> None:
    with pytest.raises(Exception, match=message):
        manifest(**overrides)


def test_targets_are_scope_checked_before_any_spawn() -> None:
    tool = ManifestTool(manifest())
    scope = ScopeContract(includes=("example.com",))
    assert tool._validated_targets({"target": "example.com"}, scope) == ["example.com"]
    with pytest.raises(Exception, match="out of scope"):
        tool._validated_targets({"target": "evil.example.org"}, scope)


def test_a_url_target_is_checked_by_its_host() -> None:
    tool = ManifestTool(
        manifest(targets={"param": "target", "kind": "url-host", "max": 1})
    )
    scope = ScopeContract(includes=("example.org",))
    assert tool._validated_targets({"target": "https://example.org/x"}, scope) == [
        "https://example.org/x"
    ]


def test_a_url_target_is_judged_by_its_whole_url() -> None:
    """`include http://box:3000` must not authorize `http://box:4000`."""
    parsed = manifest(
        params={"url": {"type": "string", "required": True}},
        argv=["-c", "x", "{url}"],
        targets={"param": "url", "kind": "url-host", "max": 1},
    )
    scope = ScopeContract(includes=("http://box.internal:3000",))
    tool = ManifestTool(parsed, StubRunner())  # type: ignore[arg-type]
    assert tool._validated_targets({"url": "http://box.internal:3000/x"}, scope)
    with pytest.raises(Exception, match="out of scope"):
        tool._validated_targets({"url": "http://box.internal:4000/x"}, scope)
    assert render_argv(parsed, {"url": "http://box.internal:3000/x"})[-1] == (
        "http://box.internal:3000/x"
    )


class StubRunner:
    def validate_invocation(self, *_a: object) -> None:
        return None

    def verify_allowlist_signature(self) -> str | None:
        return None
