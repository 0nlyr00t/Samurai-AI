from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from samurai_ai.config import sandbox_allowlist_path, vault_key_path
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.sandbox import (
    DockerSandboxRunner,
    all_profiles,
    sign_allowlist,
)
from samurai_ai.services.vault import load_master_key


def register_sandbox_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    sandbox = subcommands.add_parser("sandbox", help="Inspect and sign sandbox profiles")
    sandbox_commands = sandbox.add_subparsers(dest="sandbox_command", required=True)
    sandbox_commands.add_parser("profiles", help="List sandbox profiles and their status")
    sandbox_commands.add_parser("sign", help="Sign the static sandbox profile allowlist")
    sandbox_commands.add_parser(
        "verify", help="Check the signature without writing anything"
    )
    check = sandbox_commands.add_parser(
        "check", help="Probe whether the operation's targets are reachable at all"
    )
    check.add_argument("operation_id")


def handle_sandbox_command(args: argparse.Namespace) -> int | None:
    if args.command != "sandbox":
        return None
    if args.sandbox_command == "check":
        return _check(args)
    runner = _default_runner()
    if args.sandbox_command == "verify":
        return _verify_signature(runner, args.json_output)
    if args.sandbox_command == "sign":
        signature = sign_allowlist(runner.signature_path, load_master_key(vault_key_path()))
        if args.json_output:
            _print_json(
                {
                    "signed": len(all_profiles()),
                    "signature": signature,
                    "path": str(runner.signature_path),
                }
            )
        else:
            print(
                f"signed {len(all_profiles())} sandbox profile(s) -> "
                f"{runner.signature_path}"
            )
        return 0
    return _show_profiles(runner, args.json_output)


def _check(args: argparse.Namespace) -> int:
    """Cut a reachability question in two: host side, then container side.

    A target the host cannot reach is a route/VPN problem; a target the host
    reaches and the container does not is a network-mode problem.
    """
    from samurai_ai.cli import _make_services
    from samurai_ai.execution.sandbox import get_profile
    from samurai_ai.services.reachability import PROBE_PORTS, is_address, probe_host

    operations = _make_services(getattr(args, "db", None))[0]
    operation = operations.get(args.operation_id)
    addresses: list[tuple[str, str]] = []
    for include in operation.scope.includes:
        resolved = operation.scope.resolve_host(include)
        addresses.append((include, resolved))

    docker_ok = _docker_alive()
    print(f"docker: {'ok' if docker_ok else 'not running — nothing below can work'}")
    rows: list[tuple[str, str, str, str]] = []
    for name, address in addresses:
        if not is_address(address):
            rows.append((name, address, "n/a", "not an address"))
            continue
        for port in PROBE_PORTS:
            host_state = probe_host(address, port)
            rows.append((name, address, str(port), host_state))
    for row_name, row_address, port_text, host_state in rows:
        print(f"host\t{row_name}\t{row_address}:{port_text}\t{host_state}")
    if not docker_ok:
        return 1

    profile = get_profile("sandbox.nmap@v1")
    if profile is None:  # pragma: no cover - the profile is built in
        print("error: sandbox.nmap@v1 is missing", file=sys.stderr)
        return 1
    reachable_from_host = {
        row_address for _, row_address, _, state in rows if state == "open"
    }
    runner = _default_runner()
    verdict = 0
    for name, address in addresses:
        if not is_address(address):
            continue
        outcome = runner.run(
            profile,
            f"check{abs(hash(address)) % 10**6}",
            "nmap",
            ["-sT", "-Pn", "-p", ",".join(str(port) for port in PROBE_PORTS), address],
            operation.scope,
            network="host",
        )
        open_ports = [
            line.split("/")[0]
            for line in outcome.result.stdout.splitlines()
            if "/tcp" in line and " open " in line
        ]
        print(f"sandbox\t{name}\t{address}\t{','.join(open_ports) or 'nothing open'}")
        if not open_ports:
            verdict = 1
            if address in reachable_from_host:
                print(
                    f"  hint: {address} answers the host but not the container — "
                    f"check the VPN route and try `--network host`",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  hint: {address} does not answer the host either — the route, "
                    f"the VPN or the box itself is down",
                    file=sys.stderr,
                )
    return verdict


def _docker_alive() -> bool:
    import subprocess

    try:
        done = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _default_runner() -> DockerSandboxRunner:
    return DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
    )


def _show_profiles(runner: DockerSandboxRunner, json_output: bool) -> int:
    signature_status = runner.allowlist_status()
    resign = runner.resign_record()
    last_resign = _describe_resign(resign)
    docker_ok = asyncio.run(runner.docker_available())
    rows: list[dict[str, Any]] = []
    for profile in all_profiles():
        image_ok = asyncio.run(runner.image_present(profile.image)) if docker_ok else False
        rows.append(
            {
                "id": profile.id,
                "image": profile.image,
                "network_mode": profile.network_mode,
                "cpu_limit": profile.cpu_limit,
                "memory_mb": profile.memory_mb,
                "pids_limit": profile.pids_limit,
                "timeout_seconds": profile.timeout_seconds,
                "max_output_bytes": profile.max_output_bytes,
                "allowed_executables": list(profile.allowed_executables),
                "env_names": list(profile.env_names),
                "proxy_flag": profile.proxy_flag,
                "proxy_scheme": profile.proxy_scheme,
                "resolver_url": profile.resolver_url,
                "egress_hosts": list(profile.egress_hosts),
                "signature": signature_status,
                "last_resign": last_resign,
                "docker": "available" if docker_ok else "unavailable",
                "image_present": image_ok,
            }
        )
    if json_output:
        _print_json(rows)
        return 0
    for row in rows:
        executables = ", ".join(str(name) for name in row["allowed_executables"])
        print(
            f"{row['id']}\tnet={row['network_mode']}\tcpus={row['cpu_limit']}\t"
            f"mem={row['memory_mb']}m\tpids={row['pids_limit']}\tsig={row['signature']}\t"
            f"docker={row['docker']}\timage={'local' if row['image_present'] else 'missing'}"
        )
        if row["last_resign"]:
            print(f"  {row['last_resign']}")
        print(f"  image: {row['image']}")
        print(f"  executables: {executables}")
        if row["proxy_flag"]:
            print(f"  proxy flag: {row['proxy_flag']} ({row['proxy_scheme']})")
        if row["resolver_url"]:
            print(f"  resolver: {row['resolver_url']}")
        if row["env_names"]:
            print(f"  secret env names: {', '.join(str(name) for name in row['env_names'])}")
        if row["egress_hosts"]:
            print(f"  egress hosts: {', '.join(str(host) for host in row['egress_hosts'])}")
    return 0


def _verify_signature(runner: DockerSandboxRunner, json_output: bool) -> int:
    status = runner.allowlist_status()
    ok = status == "signed"
    if json_output:
        _print_json({"status": status, "profiles": len(all_profiles()), "ok": ok})
        return 0 if ok else 1
    if ok:
        print(f"signature matches {len(all_profiles())} sandbox profile(s)")
        return 0
    print(
        f"signature is {status}; run 'samurai sandbox sign' to re-create it",
        file=sys.stderr,
    )
    return 1


def _describe_resign(record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    reason = record.get("reason", "?")
    at = str(record.get("resigned_at", "?"))[:19]
    return f"resigned {at} ({reason}, {record.get('profiles', '?')} profile(s))"


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
