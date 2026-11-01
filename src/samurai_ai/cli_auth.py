"""`samurai auth` and `samurai role`: who is signed in and what may they do."""

from __future__ import annotations

import argparse
import sys

from samurai_ai.domain.access import AccessError, Role
from samurai_ai.services.access import AccessService


def handle_auth_command(args: argparse.Namespace, access: AccessService) -> int | None:
    if args.command != "auth":
        return None
    if args.auth_command == "configure":
        try:
            access.configure(args.issuer, args.client_id, claim=args.claim or "")
        except AccessError as exc:
            print(f"[auth] {exc}", file=sys.stderr)
            return 2
        print(f"identity provider set to {access.issuer}")
        return 0
    if args.auth_command == "login":
        try:
            code = access.begin()
        except AccessError as exc:
            print(f"[auth] {exc}", file=sys.stderr)
            return 2
        print(f"Open {code.verification_uri} and enter the code {code.user_code}")
        try:
            session = access.complete(code)
        except AccessError as exc:
            print(f"[auth] {exc}", file=sys.stderr)
            return 1
        print(f"signed in as {session.principal.label} ({session.principal.role.value})")
        return 0
    if args.auth_command == "whoami":
        try:
            principal = access.current()
        except AccessError as exc:
            print(f"[auth] {exc}", file=sys.stderr)
            return 2
        print(f"{principal.label} ({principal.role.value})")
        return 0
    # logout
    print("signed out" if access.logout() else "there was no session")
    return 0


def handle_role_command(args: argparse.Namespace, access: AccessService) -> int | None:
    if args.command != "role":
        return None
    if args.role_command == "map":
        try:
            access.map_role(args.claim_value, Role(args.role))
        except (AccessError, ValueError) as exc:
            print(f"[role] {exc}", file=sys.stderr)
            return 2
        print(f"{args.claim_value} now maps to {args.role}")
        return 0
    if args.role_command == "rm":
        print(
            f"{args.claim_value} no longer maps to a role"
            if access.access.remove_mapping(args.claim_value)
            else f"{args.claim_value} was not mapped"
        )
        return 0
    mappings = access.access.list_mappings()
    if not mappings:
        print("no claim is mapped to a role; unmapped claims get viewer")
        return 0
    for claim_value, role in mappings:
        print(f"{claim_value}\t{role.value}")
    return 0
