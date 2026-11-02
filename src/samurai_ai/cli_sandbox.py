from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from samurai_ai.config import sandbox_allowlist_path, vault_key_path
from samurai_ai.domain.sandbox import verify_profiles
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.sandbox import (
    SANDBOX_PROFILES,
    DockerSandboxRunner,
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


def handle_sandbox_command(args: argparse.Namespace) -> int | None:
    if args.command != "sandbox":
        return None
    runner = _default_runner()
    if args.sandbox_command == "sign":
        signature = sign_allowlist(runner.signature_path, load_master_key(vault_key_path()))
        if args.json_output:
            _print_json(
                {
                    "signed": len(SANDBOX_PROFILES),
                    "signature": signature,
                    "path": str(runner.signature_path),
                }
            )
        else:
            print(
                f"signed {len(SANDBOX_PROFILES)} sandbox profile(s) -> "
                f"{runner.signature_path}"
            )
        return 0
    return _show_profiles(runner, args.json_output)


def _default_runner() -> DockerSandboxRunner:
    return DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
    )


def _show_profiles(runner: DockerSandboxRunner, json_output: bool) -> int:
    signature_status = _signature_status(runner)
    docker_ok = asyncio.run(runner.docker_available())
    rows: list[dict[str, Any]] = []
    for profile in SANDBOX_PROFILES:
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
                "egress_hosts": list(profile.egress_hosts),
                "signature": signature_status,
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
        print(f"  image: {row['image']}")
        print(f"  executables: {executables}")
        if row["proxy_flag"]:
            print(f"  proxy flag: {row['proxy_flag']}")
        if row["env_names"]:
            print(f"  secret env names: {', '.join(str(name) for name in row['env_names'])}")
        if row["egress_hosts"]:
            print(f"  egress hosts: {', '.join(str(host) for host in row['egress_hosts'])}")
    return 0


def _signature_status(runner: DockerSandboxRunner) -> str:
    if not runner.signature_path.exists():
        return "unsigned"
    try:
        master_key = runner.master_key_loader()
    except Exception:
        return "unknown"
    signature = runner.signature_path.read_text(encoding="utf-8")
    return "signed" if verify_profiles(master_key, SANDBOX_PROFILES, signature) else "invalid"


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
