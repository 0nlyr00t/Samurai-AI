"""`samurai offline`: say what an air-gapped operation can and cannot do.

The mode is enforced where the calls happen; this command is the report the
operator reads *before* a run, rather than collecting refusals one by one.
"""

from __future__ import annotations

import argparse
from typing import Any

from samurai_ai.domain.execution import ToolKind
from samurai_ai.domain.offline import is_local_url, needs_internet


def register_offline_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    offline = subcommands.add_parser("offline", help="Inspect the air-gapped mode")
    offline_commands = offline.add_subparsers(dest="offline_command", required=True)
    check = offline_commands.add_parser(
        "check", help="What this offline operation can and cannot do"
    )
    check.add_argument("operation_id")


def handle_offline_command(args: argparse.Namespace, services: tuple[Any, ...]) -> int | None:
    if args.command != "offline":
        return None
    operations, models, execution = services[0], services[7], services[4]
    operation = operations.get(args.operation_id)
    if not operation.offline:
        print(
            f"operation {operation.id} is not offline; recreate it with --offline "
            "to make the harness refuse the internet"
        )
        return 0

    print("offline: model calls and outside tools are refused")
    local: list[str] = []
    remote: list[str] = []
    for model in models.list_models():
        try:
            provider = models.registry.get_provider(model.provider_id)
        except Exception:  # noqa: BLE001 - an unregistered provider is not ours to judge
            continue
        (local if is_local_url(provider.base_url) else remote).append(model.id)
    print(f"  local models: {', '.join(local) or '(none — register ollama or similar)'}")
    if remote:
        print(f"  refused models: {len(remote)} remote (e.g. {', '.join(remote[:3])})")

    blocked = sorted(
        definition.id
        for definition in execution.list_tools()
        if definition.kind is ToolKind.SANDBOX and needs_internet(definition.id)
    )
    print(f"  refused tools: {', '.join(blocked) or '(none)'}")
    print("  everything else runs against targets that are reachable without a route out")
    return 0 if local else 1


__all__ = ["handle_offline_command", "register_offline_commands"]
