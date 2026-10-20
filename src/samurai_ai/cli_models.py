from __future__ import annotations

import argparse
import json
import sys

from samurai_ai.domain.execution import ApprovalRequiredError
from samurai_ai.services.models import ModelService


def register_model_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    model = subcommands.add_parser("model", help="Inspect providers and call LLM models")
    model_commands = model.add_subparsers(dest="model_command", required=True)

    model_commands.add_parser("providers", help="List LLM providers and their status")

    list_command = model_commands.add_parser("list", help="List registered models")
    list_command.add_argument("--provider", dest="provider_id")
    list_command.add_argument(
        "--discover",
        action="store_true",
        help="Query the provider endpoint for live model discovery",
    )

    register = model_commands.add_parser(
        "register", help="Register a custom OpenAI-compatible endpoint"
    )
    register.add_argument("id")
    register.add_argument("--base-url", dest="base_url", required=True)
    register.add_argument("--secret-name", dest="secret_name")
    register.add_argument("--display-name", dest="display_name", default="")

    remove = model_commands.add_parser("remove", help="Remove a custom endpoint")
    remove.add_argument("id")

    health = model_commands.add_parser("health", help="Check provider or model health")
    health.add_argument("target")

    complete = model_commands.add_parser(
        "complete", help="Run a governed single-prompt completion"
    )
    complete.add_argument("model_id")
    complete.add_argument("--operation", dest="operation_id", required=True)
    complete.add_argument("--prompt", required=True)
    complete.add_argument("--max-tokens", dest="max_tokens", type=int)
    complete.add_argument("--approval-id", dest="approval_id")


def handle_model_command(args: argparse.Namespace, models: ModelService) -> int | None:
    if args.command != "model":
        return None
    if args.model_command == "providers":
        rows = models.list_providers()
        if args.json_output:
            _print_json(rows)
        else:
            for row in rows:
                local = "local" if row["local"] else "remote"
                state = "enabled" if row["enabled"] else "disabled"
                print(
                    f"{row['id']}\t{row['api_family']}\t{local}\t{state}\t"
                    f"auth={row['auth']}\t{row['base_url']}"
                )
        return 0
    if args.model_command == "list":
        if args.discover:
            provider_id = args.provider_id
            if provider_id is None:
                print("error: --discover requires --provider <id>", file=sys.stderr)
                return 2
            entries = models.discover_models(provider_id)
        else:
            entries = models.list_models(args.provider_id)
        if args.json_output:
            _print_json([entry.model_dump(mode="json") for entry in entries])
        else:
            for entry in entries:
                caps = ",".join(sorted(cap.value for cap in entry.capabilities)) or "-"
                cost = f"${entry.cost_input_per_m}/{entry.cost_output_per_m}perM"
                print(
                    f"{entry.id}\tctx={entry.context_limit}\tout={entry.max_output_tokens}\t"
                    f"caps={caps}\t{cost}"
                )
        return 0
    if args.model_command == "register":
        endpoint = models.register_endpoint(
            args.id, args.base_url, args.secret_name, args.display_name
        )
        if args.json_output:
            _print_json(endpoint.model_dump(mode="json"))
        else:
            print(f"registered\t{endpoint.id}\t{endpoint.base_url}")
        return 0
    if args.model_command == "remove":
        models.remove_endpoint(args.id)
        if args.json_output:
            _print_json({"removed": args.id})
        else:
            print(f"removed\t{args.id}")
        return 0
    if args.model_command == "health":
        report = models.health(args.target)
        if args.json_output:
            _print_json(report)
        else:
            state = "healthy" if report["healthy"] else "unavailable"
            print(f"{report['provider']}\t{state}\t{report['base_url']}")
        return 0 if report["healthy"] else 1
    return _complete(args, models)


def _complete(args: argparse.Namespace, models: ModelService) -> int:
    try:
        response = models.complete(
            args.operation_id,
            args.model_id,
            args.prompt,
            max_tokens=args.max_tokens,
            approval_id=args.approval_id,
        )
    except ApprovalRequiredError as exc:
        if args.json_output:
            print(
                json.dumps(
                    {"error": "approval-required", "approval_id": exc.approval_id},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(
                f"error: approval required; approve with "
                f"'samurai approval approve {exc.approval_id}' and re-run with "
                f"--approval-id {exc.approval_id}",
                file=sys.stderr,
            )
        return 2
    if args.json_output:
        _print_json(response.model_dump(mode="json"))
    else:
        print(response.text)
        print(
            f"[{response.model_id} via {response.provider_id} | "
            f"tokens={response.usage.prompt_tokens}+{response.usage.completion_tokens} | "
            f"{response.latency_ms}ms]",
            file=sys.stderr,
        )
    return 0


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
