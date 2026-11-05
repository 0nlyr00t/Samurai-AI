"""`samurai discover`: the recon chain with no model in it.

Ports, then a web probe on what answered, then a template scan on what was alive.
Deterministic and cheap enough to run in CI, so a first pass over a target does
not need an API key — the agent takes over from the artifacts afterwards.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract

#: ports a browser-shaped service usually lands on
WEB_PORTS = frozenset({80, 443, 3000, 5000, 8000, 8008, 8080, 8081, 8088, 8443,
                       8888, 9000, 9090, 10000}) | frozenset(range(8000, 8100))
#: a loopback target is the container's own loopback unless we name the host
HOST_GATEWAY = "host.docker.internal"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_OPEN_LINE = re.compile(
    r"^(?P<host>[^\s:]+):(?P<port>\d+)/(?:tcp|udp) open\s*(?P<service>.*)$",
    re.MULTILINE,
)
#: service labels nmap gives to something that speaks HTTP
_HTTP_SERVICES = re.compile(
    r"^(https?|http-alt|http-proxy|ssl/http|http-mgmt|nginx|apache|tomcat)", re.I
)
#: one stage may not exceed what the adapters accept
STAGE_LIMIT = 10


def register_discover_command(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    discover = subcommands.add_parser(
        "discover", help="Recon chain without a model: ports, web probe, templates"
    )
    discover.add_argument("targets", nargs="+", help="in-scope hosts or URLs")
    discover.add_argument(
        "--mode",
        default=OperationMode.CTF_HTB.value,
        choices=[mode.value for mode in OperationMode],
    )
    discover.add_argument("--ports", help="port spec for nmap (default: top 100)")
    discover.add_argument(
        "--severity",
        action="append",
        choices=["critical", "high", "medium", "low", "info"],
        help="nuclei severity; repeatable (default: critical, high, medium)",
    )
    discover.add_argument("--no-templates", action="store_true", help="stop after the web probe")
    discover.add_argument("--offline", action="store_true")
    discover.add_argument("--yes", "-y", action="store_true", help="Accept the derived scope")
    discover.set_defaults(handler=_discover)


def _auto_approve(execution: Any, operation_id: str, label: str, risk: Any) -> str:
    """No model outranks the operator here, so the chain approves itself."""
    approval = execution.kernel.request_approval(
        operation_id, label, risk, reason="discover chain", requested_by="discover"
    )
    execution.kernel.approve(approval.id, decided_by="discover")
    return str(approval.id)


def _summary(result: Any) -> str:
    """The parsed part of a tool answer, without the raw dump behind it."""
    text = str(result.stdout)
    if "# raw" in text:
        text = text.split("# raw", 1)[0]
    return text.replace("# summary", "").strip()


def _open_ports(summary: str) -> dict[str, dict[int, str]]:
    """`host:port/tcp open <service>` lines from the nmap summary, per host."""
    found: dict[str, dict[int, str]] = {}
    for match in _OPEN_LINE.finditer(summary):
        found.setdefault(match["host"], {})[int(match["port"])] = match["service"]
    return found


def _is_web(port: int, service: str) -> bool:
    """A known web port, or whatever nmap recognised as an HTTP service."""
    return port in WEB_PORTS or bool(_HTTP_SERVICES.match(service))


def _origin(host: str, port: int) -> str:
    if port == 443:
        return f"https://{host}"
    if port == 80:
        return f"http://{host}"
    return f"http://{host}:{port}"


def _web_urls(hosts: dict[str, dict[int, str]], name: str | None = None) -> list[str]:
    """Probe candidates, HTTP-labelled ports first.

    Every open port is a candidate: a service can answer HTTP anywhere, and a
    fast nmap pass only guesses from a port-number table. httpx drops what does
    not answer HTTP, so the cost of a wrong guess is one timeout.
    """
    likely: list[str] = []
    rest: list[str] = []
    for host, ports in hosts.items():
        for port in sorted(ports):
            # nmap answers with the address it resolved; a gateway name would be
            # lost, and with it the scope rule that allowed the scan
            label = name or host
            (likely if _is_web(port, ports[port]) else rest).append(_origin(label, port))
    return (likely + rest)[:STAGE_LIMIT]


def _host_of(target: str) -> str:
    """The address a container can scan, from a host or a URL argument."""
    if "://" in target:
        host = urlsplit(target).hostname or target
    else:
        host = target
    return HOST_GATEWAY if host in LOOPBACK else host


def _for_container(target: str) -> str:
    """A container reaches the host loopback only under its gateway name."""
    if "://" in target:
        parts = urlsplit(target)
        if (parts.hostname or "") not in LOOPBACK:
            return target
        netloc = HOST_GATEWAY + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    return _host_of(target)


def _run(execution: Any, operation_id: str, tool_id: str, params: dict[str, Any]) -> Any:
    definition = execution.registry.get(tool_id).definition
    approval = _auto_approve(execution, operation_id, f"tool.run {tool_id}", definition.risk_class)
    return execution.run_tool(operation_id, tool_id, params, approval_id=approval)


def _stage(
    execution: Any, operation_id: str, tool_id: str, params: dict[str, Any]
) -> Any | None:
    """One stage; a refusal or a crash is reported and costs the target only."""
    try:
        return _run(execution, operation_id, tool_id, params)
    except Exception as exc:  # noqa: BLE001 - any stage failure is reported, not raised
        print(f"error: {tool_id}: {exc}", file=sys.stderr)
        return None


def _discover(args: argparse.Namespace, execution: Any, operations: Any) -> int:
    # the gateway name resolves inside docker but not on a public resolver, so
    # it is declared as an alias of the loopback it stands for
    loopback = any(_for_container(target) != target for target in args.targets)
    hosts = {HOST_GATEWAY: "127.0.0.1"} if loopback else {}
    try:
        scope = ScopeContract(includes=tuple(args.targets), hosts=hosts)
    except Exception as exc:  # noqa: BLE001 - pydantic reports the same class of error
        print(f"error: {exc}", file=sys.stderr)
        return 2
    operation = operations.create(
        "discover " + " ".join(args.targets[:2]),
        OperationMode(args.mode),
        scope,
        offline=args.offline,
        objective="deterministic reconnaissance",
    )
    print(f"operation\t{operation.id}")
    if not sys.stdin.isatty() and not args.yes:
        print("scope\ttaken from arguments", file=sys.stderr)

    failed = 0
    for target in args.targets:
        print(f"\n== {target}")
        # a URL argument names a host to scan and the origin to probe
        scan_host = _host_of(target)
        explicit = _for_container(target) if "://" in target else None
        scan_params: dict[str, Any] = {"targets": [scan_host]}
        if args.ports:
            scan_params["ports"] = args.ports
        scan = _stage(execution, operation.id, "scan.nmap", scan_params)
        if scan is None:
            failed += 1
            continue
        print(_summary(scan) or "(no output)")
        if scan.run.status.value != "completed":
            failed += 1
            continue
        hint = scan_host if scan_host == HOST_GATEWAY else None
        urls = [explicit] if explicit else _web_urls(_open_ports(_summary(scan)), hint)
        if not urls:
            print("no web ports open")
            continue
        print(f"\n-- web probe on {len(urls)} target(s)")
        probe = _run(execution, operation.id, "recon.httpx-probe",
                     {"targets": urls, "profile": "full"})
        print(_summary(probe) or "(no output)")
        if args.no_templates:
            continue
        if probe.run.status.value != "completed":
            failed += 1
            continue
        severity = args.severity or ["critical", "high", "medium"]
        print(f"\n-- templates on {len(urls)} target(s), severity={'/'.join(severity)}")
        templates = _run(execution, operation.id, "scan.nuclei",
                         {"targets": urls, "severity": severity})
        print(_summary(templates) or "(no findings)")

    print(f"\nartifacts\tsamurai artifact list {operation.id}")
    return 1 if failed else 0


def handle_discover_command(
    args: argparse.Namespace, execution: Any, operations: Any
) -> int | None:
    if getattr(args, "command", None) != "discover":
        return None
    return _discover(args, execution, operations)


__all__ = ["handle_discover_command", "register_discover_command"]
