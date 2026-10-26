from __future__ import annotations

import argparse
import json
import sys

from samurai_ai.agents.budgets import ModelBudget
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

    add = model_commands.add_parser(
        "add",
        help="Whitelist a custom model name on a provider "
        "(accepts names with slashes; @v1 is appended when omitted)",
    )
    add.add_argument("model_id")
    add.add_argument("--display-name", dest="display_name", default="")
    add.add_argument("--context-limit", dest="context_limit", type=int, default=32768)
    add.add_argument(
        "--max-output-tokens", dest="max_output_tokens", type=int, default=4096
    )

    remove = model_commands.add_parser(
        "remove", help="Remove a custom endpoint or custom model (id with '/')"
    )
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
    complete.add_argument(
        "--fallback",
        action="append",
        dest="fallbacks",
        default=[],
        metavar="MODEL_ID",
        help="Fallback model id (repeatable, tried in order)",
    )

    budget = model_commands.add_parser(
        "budget", help="Show or set the per-operation model call budget"
    )
    budget.add_argument("operation_id")
    budget.add_argument("--max-tokens", dest="max_tokens", type=int)
    budget.add_argument("--max-cost", dest="max_cost", type=float)
    budget.add_argument("--max-calls", dest="max_calls", type=int)

    usage = model_commands.add_parser(
        "usage", help="Show aggregated model usage for an operation"
    )
    usage.add_argument("operation_id")


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
    if args.model_command == "add":
        model = models.add_model(
            args.model_id,
            display_name=args.display_name,
            context_limit=args.context_limit,
            max_output_tokens=args.max_output_tokens,
        )
        if args.json_output:
            _print_json(model.model_dump(mode="json"))
        else:
            print(f"added\t{model.id}\tprovider={model.provider_id}")
        return 0
    if args.model_command == "remove":
        if "/" in args.id:
            models.remove_model(args.id)
            if args.json_output:
                _print_json({"removed": args.id})
            else:
                print(f"removed\t{args.id}")
            return 0
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
    if args.model_command == "budget":
        return _budget(args, models)
    if args.model_command == "usage":
        summary = models.usage(args.operation_id)
        if args.json_output:
            _print_json(summary.model_dump(mode="json"))
        else:
            print(
                f"calls={summary.calls}\ttokens={summary.prompt_tokens}+"
                f"{summary.completion_tokens}\tcost=${summary.cost_usd}"
            )
            for model_id, usage_entry in summary.by_model.items():
                print(
                    f"  {model_id}\tcalls={usage_entry['calls']}\t"
                    f"tokens={usage_entry['prompt_tokens']}+{usage_entry['completion_tokens']}\t"
                    f"cost=${usage_entry['cost_usd']}"
                )
        return 0
    return _complete(args, models)


def _budget(args: argparse.Namespace, models: ModelService) -> int:
    if (
        args.max_tokens is None
        and args.max_cost is None
        and args.max_calls is None
    ):
        budget = models.get_budget(args.operation_id)
        if args.json_output:
            _print_json(budget.model_dump(mode="json"))
        elif budget.is_unset():
            print("no budget set")
        else:
            print(
                f"max-tokens={budget.max_tokens_total or '-'}\t"
                f"max-cost={budget.max_cost_usd or '-'}\t"
                f"max-calls={budget.max_calls or '-'}"
            )
        return 0
    budget = models.set_budget(
        args.operation_id,
        ModelBudget(
            max_tokens_total=args.max_tokens,
            max_cost_usd=args.max_cost,
            max_calls=args.max_calls,
        ),
    )
    if args.json_output:
        _print_json(budget.model_dump(mode="json"))
    else:
        print(f"budget set for {args.operation_id}")
    return 0


def _complete(args: argparse.Namespace, models: ModelService) -> int:
    try:
        response = models.complete(
            args.operation_id,
            args.model_id,
            args.prompt,
            max_tokens=args.max_tokens,
            approval_id=args.approval_id,
            fallbacks=tuple(args.fallbacks),
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
        route = response.model_id
        if route != args.model_id:
            route = f"{args.model_id} -> {route}"
        print(
            f"[{route} via {response.provider_id} | "
            f"tokens={response.usage.prompt_tokens}+{response.usage.completion_tokens} | "
            f"{response.latency_ms}ms]",
            file=sys.stderr,
        )
    return 0


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
