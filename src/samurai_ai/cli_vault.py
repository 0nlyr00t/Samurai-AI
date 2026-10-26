from __future__ import annotations

import argparse
import getpass
import json
import sys

from samurai_ai.domain.vault import SecretMetadata, VaultError
from samurai_ai.services.vault import VaultService


def register_vault_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    secret = subcommands.add_parser("secret", help="Manage secrets vault entries")
    secret_commands = secret.add_subparsers(dest="secret_command", required=True)

    add = secret_commands.add_parser("add", help="Add an encrypted secret")
    add.add_argument("name")
    add.add_argument("--operation", dest="operation_id")
    add.add_argument("--value", dest="value")
    add.add_argument(
        "--stdin",
        action="store_true",
        dest="read_stdin",
        help="Read the secret value from stdin",
    )

    list_command = secret_commands.add_parser("list", help="List secret metadata")
    list_command.add_argument("--operation", dest="operation_id")

    remove = secret_commands.add_parser("remove", help="Remove a secret")
    remove.add_argument("name")
    remove.add_argument("--operation", dest="operation_id")


def handle_vault_command(args: argparse.Namespace, vault: VaultService) -> int | None:
    if args.command != "secret":
        return None
    if args.secret_command == "add":
        metadata = vault.add(
            args.name, _read_value(args), operation_id=args.operation_id
        )
        if args.json_output:
            _print_json(metadata.model_dump(mode="json"))
        else:
            _print_metadata(metadata)
        return 0
    if args.secret_command == "list":
        entries = vault.list_metadata(args.operation_id)
        if args.json_output:
            _print_json([entry.model_dump(mode="json") for entry in entries])
        else:
            for entry in entries:
                _print_metadata(entry)
        return 0
    vault.remove(args.name, operation_id=args.operation_id)
    if args.json_output:
        _print_json({"removed": args.name, "operation_id": args.operation_id})
    else:
        print(f"removed\t{args.name}\t{args.operation_id or 'global'}")
    return 0


def _read_value(args: argparse.Namespace) -> str:
    if args.value is not None and args.read_stdin:
        raise VaultError("use either --value or --stdin, not both")
    if args.value is not None:
        return str(args.value)
    if args.read_stdin or not sys.stdin.isatty():
        if sys.stdin.isatty():
            return getpass.getpass("Secret value: ")
        value: str = sys.stdin.read()
        return value.rstrip("\r\n")
    raise VaultError(
        "no secret value given; pass --value, --stdin or pipe the value via stdin"
    )


def _print_metadata(metadata: SecretMetadata) -> None:
    scope = metadata.operation_id or "global"
    print(
        f"{metadata.id}\t{metadata.name}\tscope={scope}\t"
        f"by={metadata.created_by}\t{metadata.created_at.isoformat()}"
    )


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
