"""A non-http(s) scheme on an http-only tool is an input error, not out of scope.

Regression for the live HTB run where the agent fed `ssh://10.129.141.194`
(an in-scope box) into http-only tools four times and each time read back
"... is outside the operation scope: only http and https targets are
supported" — a scope message for what is really a misrouted call.
"""

import pytest

from samurai_ai.domain.execution import ToolScopeError, ToolValidationError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.scope_guard import ScopeGuardError, guard
from samurai_ai.domain.tool_manifest import ToolManifest
from samurai_ai.domain.url_scheme import named_scheme, require_http_scheme
from samurai_ai.execution.manifests import ManifestTool
from samurai_ai.execution.security import HttpxProbeTool, ResearchReadTool

IN_SCOPE = "10.129.141.194"
SCOPE = ScopeContract(includes=(IN_SCOPE, "example.com"))


# --- the helper -------------------------------------------------------------


def test_named_scheme_ignores_bare_hosts_and_host_port() -> None:
    # a bare host, an IP, or host:port names no scheme (urlsplit would read the
    # host of `example.com:8443` as a scheme otherwise)
    assert named_scheme("10.129.141.194") == ""
    assert named_scheme("example.com") == ""
    assert named_scheme("example.com:8443") == ""
    assert named_scheme("ssh://10.129.141.194") == "ssh"
    assert named_scheme("https://example.com/x") == "https"


def test_require_http_scheme_points_ssh_and_rdp_at_their_tools() -> None:
    with pytest.raises(ToolValidationError, match="remote.ssh-pass") as ssh:
        require_http_scheme("recon.httpx-probe", "targets", "ssh://10.129.141.194")
    assert "ssh" in str(ssh.value)
    assert "scope" not in str(ssh.value).lower()

    with pytest.raises(ToolValidationError, match="remote.rdp-check"):
        require_http_scheme("recon.httpx-probe", "targets", "rdp://10.129.141.194")

    # an unmapped scheme still fails clearly, just without a tool pointer
    with pytest.raises(ToolValidationError, match="ftp.*http"):
        require_http_scheme("recon.httpx-probe", "targets", "ftp://host/x")


def test_require_http_scheme_passes_http_and_bare_hosts() -> None:
    for value in ("10.129.141.194", "example.com", "example.com:8443",
                  "http://example.com", "https://example.com/x"):
        require_http_scheme("recon.httpx-probe", "targets", value)  # no raise


# --- http-only adapters -----------------------------------------------------


def test_httpx_probe_rejects_ssh_scheme_as_validation() -> None:
    tool = HttpxProbeTool()
    with pytest.raises(ToolValidationError, match="ssh.*remote.ssh-pass"):
        tool._validated_targets({"targets": [f"ssh://{IN_SCOPE}"]}, SCOPE)


def test_httpx_probe_rejects_ftp_scheme() -> None:
    tool = HttpxProbeTool()
    with pytest.raises(ToolValidationError, match="ftp"):
        tool._validated_targets({"targets": [f"ftp://{IN_SCOPE}/x"]}, SCOPE)


def test_httpx_probe_accepts_bare_host_and_http() -> None:
    tool = HttpxProbeTool()
    assert tool._validated_targets({"targets": [IN_SCOPE]}, SCOPE) == [IN_SCOPE]
    url = f"http://{IN_SCOPE}"
    assert tool._validated_targets({"targets": [url]}, SCOPE) == [url]


def test_httpx_probe_out_of_scope_host_is_still_a_scope_error() -> None:
    tool = HttpxProbeTool()
    with pytest.raises(ToolScopeError, match="out of scope"):
        tool._validated_targets({"targets": ["10.0.0.9"]}, SCOPE)


def test_research_read_rejects_ssh_scheme_as_validation() -> None:
    tool = ResearchReadTool()
    with pytest.raises(ToolValidationError, match="ssh.*remote.ssh-pass"):
        tool._validated_targets({"url": f"ssh://{IN_SCOPE}"}, SCOPE)


# --- the central guard ------------------------------------------------------


def test_guard_judges_a_non_http_scheme_by_its_host() -> None:
    # the ssh:// box is in scope, so the guard must not refuse it — the scheme
    # is the adapter's problem, the host is the guard's
    guard(SCOPE, {"command": f"ssh://{IN_SCOPE}"})


def test_guard_still_refuses_a_non_http_scheme_to_an_outside_host() -> None:
    with pytest.raises(ScopeGuardError, match="10.0.0.9") as exc:
        guard(SCOPE, {"command": "ssh://10.0.0.9"})
    # and it is reported as a scope problem about the host, not the old
    # "only http and https targets are supported" scheme message
    assert "only http and https" not in str(exc.value)


# --- url-host manifests -----------------------------------------------------


def _url_manifest() -> ToolManifest:
    return ToolManifest.model_validate(
        {
            "id": "web.probe",
            "title": "Demo",
            "image": "busybox@sha256:" + "a" * 64,
            "executable": "curl",
            "risk_class": "r2",
            "network": "direct",
            "params": {"url": {"type": "string", "required": True, "max_length": 400}},
            "targets": {"param": "url", "kind": "url-host", "max": 1},
            "argv": ["{url}"],
            "parser": "text-lines",
        }
    )


def test_manifest_url_host_rejects_ssh_scheme() -> None:
    tool = ManifestTool(_url_manifest())
    scope = ScopeContract(includes=(IN_SCOPE,))
    with pytest.raises(ToolValidationError, match="remote.ssh-pass"):
        tool._validated_targets({"url": f"ssh://{IN_SCOPE}"}, scope)
