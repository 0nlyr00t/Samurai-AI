"""`discover` chains the stages itself: the arithmetic is what to pin down."""

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from samurai_ai.cli_discover import (
    _discover,
    _for_container,
    _host_of,
    _open_ports,
    _web_urls,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract


def test_an_open_port_line_is_read_per_host() -> None:
    summary = (
        "10.0.0.5:22/tcp open ssh\n"
        "10.0.0.5:8080/tcp open http-proxy\n"
        "10.0.0.6:443/tcp open https\n"
        "10.0.0.6:445/tcp closed microsoft-ds\n"
    )
    assert _open_ports(summary) == {
        "10.0.0.5": {22: "ssh", 8080: "http-proxy"},
        "10.0.0.6": {443: "https"},
    }


def test_an_unusual_port_is_still_probed_after_the_likely_ones() -> None:
    """nmap's service column is a guess; a missed web port costs a whole stage."""
    assert _web_urls({"10.0.0.5": {5432: "postgresql", 3001: "http"}}) == [
        "http://10.0.0.5:3001", "http://10.0.0.5:5432"
    ]


def test_http_labelled_ports_come_first_and_default_ports_stay_clean() -> None:
    urls = _web_urls({"10.0.0.5": {22: "ssh", 80: "http", 443: "https", 8080: "http-proxy"}})
    assert urls == [
        "http://10.0.0.5", "https://10.0.0.5", "http://10.0.0.5:8080", "http://10.0.0.5:22"
    ]


def test_a_host_loopback_target_is_named_by_its_gateway() -> None:
    """The container's own 127.0.0.1 is not the host's; only the name is."""
    assert _for_container("127.0.0.1") == "host.docker.internal"
    assert _for_container("http://localhost:3000/a?b=1") == (
        "http://host.docker.internal:3000/a?b=1"
    )
    assert _host_of("https://127.0.0.1:8443/login") == "host.docker.internal"


def test_an_ordinary_target_is_left_alone() -> None:
    assert _for_container("http://example.com:8080/x") == "http://example.com:8080/x"
    assert _host_of("10.10.10.5") == "10.10.10.5"


class FakeExecution:
    """Records the chain and answers with canned summaries."""

    def __init__(self, summaries: dict[str, Any]) -> None:
        self.summaries = summaries
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.kernel = SimpleNamespace(
            request_approval=lambda *a, **k: SimpleNamespace(id="ap_1"),
            approve=lambda *a, **k: None,
        )
        self.registry = SimpleNamespace(
            get=lambda tool_id, version=None: SimpleNamespace(
                definition=SimpleNamespace(risk_class=RiskClass.R2, id=tool_id)
            )
        )

    def run_tool(self, _operation: str, tool_id: str, params: dict[str, Any], **_k: Any) -> Any:
        self.calls.append((tool_id, params))
        stdout, status = self.summaries[tool_id]
        return SimpleNamespace(
            stdout=stdout, run=SimpleNamespace(status=SimpleNamespace(value=status))
        )


class FakeOperations:
    def __init__(self) -> None:
        self.created: list[ScopeContract] = []

    def create(self, name: str, mode: OperationMode, scope: ScopeContract, **_: Any) -> Any:
        self.created.append(scope)
        return SimpleNamespace(id="op_1")


def args(*targets: str, **extra: Any) -> argparse.Namespace:
    base = {
        "targets": list(targets),
        "mode": "ctf-htb",
        "ports": "80,8080",
        "severity": None,
        "no_templates": False,
        "offline": False,
        "yes": True,
    }
    return argparse.Namespace(**{**base, **extra})


def test_the_chain_runs_ports_then_probe_then_templates(
    tmp_path: Path, capsys
) -> None:
    execution = FakeExecution({
        "scan.nmap": ("10.0.0.5:8080/tcp open http-proxy\n", "completed"),
        "recon.httpx-probe": ("http://10.0.0.5:8080\tstatus=200", "completed"),
        "scan.nuclei": ("[cve-2021-1] http://10.0.0.5:8080", "completed"),
    })
    operations = FakeOperations()
    assert _discover(args("10.0.0.5"), execution, operations) == 0
    assert [call[0] for call in execution.calls] == [
        "scan.nmap", "recon.httpx-probe", "scan.nuclei",
    ]
    assert execution.calls[1][1]["targets"] == ["http://10.0.0.5:8080"]
    assert execution.calls[2][1]["severity"] == ["critical", "high", "medium"]
    out = capsys.readouterr().out
    assert "cve-2021-1" in out


def test_no_templates_stops_after_the_probe(capsys) -> None:
    execution = FakeExecution({
        "scan.nmap": ("10.0.0.5:80/tcp open http\n", "completed"),
        "recon.httpx-probe": ("http://10.0.0.5\tstatus=200", "completed"),
    })
    assert _discover(args("10.0.0.5", no_templates=True), execution, FakeOperations()) == 0
    assert [call[0] for call in execution.calls] == ["scan.nmap", "recon.httpx-probe"]


def test_a_portless_scan_means_no_probe(capsys) -> None:
    execution = FakeExecution({"scan.nmap": ("no open ports", "completed")})
    assert _discover(args("10.0.0.5"), execution, FakeOperations()) == 0
    assert [call[0] for call in execution.calls] == ["scan.nmap"]
    assert "no web ports open" in capsys.readouterr().out


def test_a_failed_scan_fails_the_chain_and_skips_the_rest(capsys) -> None:
    execution = FakeExecution({"scan.nmap": ("boom", "failed")})
    assert _discover(args("10.0.0.5"), execution, FakeOperations()) == 1
    assert [call[0] for call in execution.calls] == ["scan.nmap"]


def test_a_loopback_target_declares_the_gateway_as_an_alias(capsys) -> None:
    """The gateway name is docker-only DNS; an alias says what it stands for."""
    operations = FakeOperations()
    execution = FakeExecution({"scan.nmap": ("", "completed")})
    assert _discover(args("127.0.0.1"), execution, operations) == 0
    assert operations.created[0].hosts == {"host.docker.internal": "127.0.0.1"}
    assert execution.calls[0][1]["targets"] == ["host.docker.internal"]


def test_an_unparsable_target_is_a_usage_error(capsys) -> None:
    assert _discover(args("not a target"), FakeExecution({}), FakeOperations()) == 2
    assert pytest.approx(2) == 2
    assert "error:" in capsys.readouterr().err
