"""Plugin packages: install, inspect, check and remove.

A plugin is the unit of extension and the unit of removal: one `install` brings
tools, playbooks and profiles, one `rm` takes them all back.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from samurai_ai.config import (
    default_database_path,
    plugins_directory,
    profiles_directory,
    tools_directory,
)
from samurai_ai.services.manifests import ManifestStore
from samurai_ai.services.plugins import PluginStore, PluginStoreError, validate_package
from samurai_ai.storage.database import Database
from samurai_ai.storage.plugins import PluginRepository


def register_plugin_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    plugin = subcommands.add_parser("plugin", help="Manage plugin packages")
    plugin_commands = plugin.add_subparsers(dest="plugin_command", required=True)

    install = plugin_commands.add_parser(
        "install", help="Install a plugin package (tools, playbooks, profiles)"
    )
    install.add_argument("path", type=Path)
    install.add_argument(
        "--trusted", action="store_true", help="required: you read the package"
    )

    plugin_commands.add_parser("list", help="List installed plugins")

    show = plugin_commands.add_parser("show", help="Show what a plugin brought")
    show.add_argument("name")

    check = plugin_commands.add_parser(
        "check", help="Re-hash installed files; a mismatch shows as tampered"
    )
    check.add_argument("name", nargs="?")

    remove = plugin_commands.add_parser("rm", help="Remove a plugin and all it brought")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true", help="do not ask")


def handle_plugin_command(
    args: argparse.Namespace, services: tuple[Any, ...]
) -> int | None:
    if args.command != "plugin":
        return None
    store = _store(getattr(args, "db", None), services)
    if args.plugin_command == "install":
        return _install(args, store)
    if args.plugin_command == "list":
        return _list(args, store)
    if args.plugin_command == "show":
        return _show(args, store)
    if args.plugin_command == "check":
        return _check(args, store)
    return _remove(args, store)


def _store(database_path: str | None, services: tuple[Any, ...]) -> PluginStore:
    database = Path(database_path) if database_path else default_database_path()
    return PluginStore(
        plugins_directory(),
        ManifestStore(tools_directory()),
        services[9],  # playbooks
        profiles_directory(),
        PluginRepository(Database(database)),
    )


def _install(args: argparse.Namespace, store: PluginStore) -> int:
    # read the package twice on purpose: validate prints the trust surface, then
    # the store installs the very same directory
    try:
        package = validate_package(args.path)
    except PluginStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"package {package.name} {package.manifest.version}: "
        f"{len(package.tools)} tool(s), {len(package.playbooks)} playbook(s), "
        f"{len(package.profiles)} profile(s)"
    )
    for tool in package.tools:
        print(f"  tool     {tool.id}\t{tool.image}")
    for profile in package.profiles:
        print(f"  profile  {profile.id}\t{profile.image}")
    for playbook in package.playbooks:
        print(f"  playbook {playbook.name}")
    try:
        installed = store.install(args.path, trusted=args.trusted)
    except (PluginStoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"installed {installed.name} {installed.version} into {installed.directory}")
    return 0


def _list(args: argparse.Namespace, store: PluginStore) -> int:
    rows = store.repository.list_all()
    if args.json_output:
        _print_json(
            [
                {
                    "name": row.name,
                    "version": row.version,
                    "installed_at": row.installed_at,
                    "tampered": store.integrity(row.name),
                }
                for row in rows
            ]
        )
        return 0
    if not rows:
        print("no plugins installed")
        return 0
    for row in rows:
        broken = store.integrity(row.name)
        mark = f"\tTAMPERED ({len(broken)} file(s))" if broken else ""
        print(f"{row.name}\t{row.version}\t{row.installed_at}{mark}")
    return 0


def _show(args: argparse.Namespace, store: PluginStore) -> int:
    row = store.repository.get(args.name)
    if row is None:
        print(f"error: plugin {args.name} is not installed", file=sys.stderr)
        return 1
    files = [{"path": item.path, "kind": item.kind} for item in store.repository.files(args.name)]
    broken = store.integrity(args.name)
    assets = [{"kind": kind, "id": ident} for kind, ident in store.repository.assets(args.name)]
    payload = {
        "name": row.name,
        "version": row.version,
        "description": row.description,
        "author": row.author,
        "installed_at": row.installed_at,
        "files": files,
        "assets": assets,
        "tampered": broken,
    }
    if args.json_output:
        _print_json(payload)
        return 0
    print(f"{row.name} {row.version}\t{row.description or '(no description)'}")
    if row.author:
        print(f"author: {row.author}")
    for item in assets:
        print(f"  brings {item['kind']}\t{item['id']}")
    for item in files:
        print(f"  file {item['kind']}\t{item['path']}")
    print(f"integrity: {'tampered: ' + ', '.join(broken) if broken else 'ok'}")
    return 0


def _check(args: argparse.Namespace, store: PluginStore) -> int:
    names = [args.name] if args.name else [row.name for row in store.repository.list_all()]
    if not names:
        print("no plugins installed")
        return 0
    bad = 0
    for name in names:
        if store.repository.get(name) is None:
            print(f"error: plugin {name} is not installed", file=sys.stderr)
            return 2
        broken = store.integrity(name)
        if broken:
            bad += 1
            print(f"{name}\tTAMPERED\t{', '.join(broken)}")
        else:
            print(f"{name}\tok")
    return 1 if bad else 0


def _remove(args: argparse.Namespace, store: PluginStore) -> int:
    row = store.repository.get(args.name)
    if row is None:
        print(f"error: plugin {args.name} is not installed", file=sys.stderr)
        return 1
    assets = store.repository.assets(args.name)
    print(f"removing {row.name} {row.version} takes back:")
    for kind, ident in assets:
        print(f"  {kind}\t{ident}")
    if not args.yes and sys.stdin.isatty():
        print("continue? [y/N] ", end="", flush=True)
        if input().strip().lower() not in {"y", "yes"}:
            print("left in place")
            return 0
    store.remove(args.name)
    print(f"removed {row.name}")
    return 0


def _print_json(payload: object) -> None:
    import json

    print(json.dumps(payload, ensure_ascii=False, indent=2))


__all__ = ["handle_plugin_command", "register_plugin_commands"]
