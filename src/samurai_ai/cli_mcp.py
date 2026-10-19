from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from samurai_ai.cli_execution import emit_tool_result, parse_tool_params
from samurai_ai.domain.execution import ApprovalRequiredError
from samurai_ai.domain.mcp import McpServer, McpValidationError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.mcp import McpService
from samurai_ai.services.operations import OperationService


def register_mcp_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    mcp = subcommands.add_parser("mcp", help="Manage MCP servers and invoke MCP tools")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)

    add = mcp_commands.add_parser("add", help="Register an MCP server (disabled by default)")
    add.add_argument("name")
    add.add_argument("--transport", choices=["stdio", "http"], required=True)
    add.add_argument(
        "--command",
        dest="command_argv",
        help="JSON argv array for stdio servers",
    )
    add.add_argument("--url", dest="url", help="Endpoint URL for http servers")

    remove = mcp_commands.add_parser("remove", help="Remove an MCP server")
    remove.add_argument("name")

    mcp_commands.add_parser("list", help="List registered MCP servers")

    for verb in ("enable", "disable"):
        toggle = mcp_commands.add_parser(verb, help=f"{verb.capitalize()} an MCP server")
        toggle.add_argument("name")

    health = mcp_commands.add_parser("health", help="Initialize handshake and ping")
    health.add_argument("name")
    health.add_argument("--operation", dest="operation_id")

    tools = mcp_commands.add_parser(
        "tools", help="Fetch and store the server's tool schemas (deferred import)"
    )
    tools.add_argument("name")
    tools.add_argument("--operation", dest="operation_id")

    invoke = mcp_commands.add_parser(
        "invoke", help="Invoke an MCP tool under governance (approval-gated, R2)"
    )
    invoke.add_argument("server")
    invoke.add_argument("tool")
    invoke.add_argument("--operation", dest="operation_id", required=True)
    invoke.add_argument(
        "--param",
        action="append",
        dest="params",
        default=[],
        metavar="KEY=VALUE",
        help="Tool parameter; VALUE is parsed as JSON when possible",
    )
    invoke.add_argument("--approval-id", dest="approval_id")


def handle_mcp_command(
    args: argparse.Namespace,
    mcp: McpService,
    operations: OperationService,
    execution: ToolExecutionService,
) -> int | None:
    if args.command != "mcp":
        return None
    if args.mcp_command == "add":
        server = mcp.add(
            args.name,
            args.transport,
            command=_parse_command(args.command_argv),
            url=args.url,
        )
        _emit_server(server, args.json_output)
        return 0
    if args.mcp_command == "remove":
        mcp.remove(args.name)
        if args.json_output:
            _print_json({"removed": args.name})
        else:
            print(f"removed\t{args.name}")
        return 0
    if args.mcp_command == "list":
        servers = mcp.list_all()
        if args.json_output:
            _print_json([_server_payload(server) for server in servers])
        else:
            for server in servers:
                print(_server_line(server))
        return 0
    if args.mcp_command in ("enable", "disable"):
        server = mcp.set_enabled(args.name, args.mcp_command == "enable")
        _emit_server(server, args.json_output)
        return 0
    if args.mcp_command == "health":
        report = mcp.health(args.name, _scope_for(args, mcp, operations))
        if args.json_output:
            _print_json(report)
        else:
            info = report["server_info"]
            print(
                f"{report['server']}\t{report['transport']}\t"
                f"protocol={report['protocol']}\tping=ok\t"
                f"server-info={info.get('name', '-')} {info.get('version', '-')}"
            )
        return 0
    if args.mcp_command == "tools":
        tools = mcp.fetch_tools(args.name, _scope_for(args, mcp, operations))
        if args.json_output:
            _print_json([tool.model_dump(mode="json") for tool in tools])
        else:
            for tool in tools:
                print(f"mcp.{args.name}.{tool.name}\t{tool.description or '-'}")
            print(f"stored {len(tools)} tool schema(s) for {args.name}")
        return 0
    return _invoke(args, mcp, execution)


def _invoke(
    args: argparse.Namespace, mcp: McpService, execution: ToolExecutionService
) -> int:
    server = mcp.get(args.server)
    params = parse_tool_params(args.params)
    tool_id = f"mcp.{server.name}.{args.tool}"
    try:
        result = execution.run_tool(
            args.operation_id,
            tool_id,
            params,
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
    emit_tool_result(result, args.json_output)
    return 0 if result.run.exit_code == 0 else 1


def _scope_for(
    args: argparse.Namespace, mcp: McpService, operations: OperationService
) -> ScopeContract | None:
    server = mcp.get(args.name)
    if server.transport != "http":
        return None
    if not args.operation_id:
        raise McpValidationError(
            f"http MCP server {server.name!r} requires --operation <id> so the "
            "operation scope is enforced on the endpoint"
        )
    return operations.get(args.operation_id).scope


def _parse_command(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(part, str) for part in parsed):
        raise McpValidationError("--command must be a JSON array of strings")
    if not parsed:
        raise McpValidationError("--command must not be empty")
    return tuple(parsed)


def _server_payload(server: McpServer) -> dict[str, Any]:
    return server.model_dump(mode="json")


def _server_line(server: McpServer) -> str:
    target = server.url or " ".join(server.command or ())
    state = "enabled" if server.enabled else "disabled"
    return (
        f"{server.name}\t{server.transport}\t{state}\ttrust={server.trust_level}\t{target}"
    )


def _emit_server(server: McpServer, json_output: bool) -> None:
    if json_output:
        _print_json(_server_payload(server))
    else:
        print(_server_line(server))


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
