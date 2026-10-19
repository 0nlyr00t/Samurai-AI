from __future__ import annotations

import asyncio
from typing import Any

from samurai_ai.domain.execution import ToolDefinition
from samurai_ai.domain.mcp import (
    MCP_PROTOCOL_VERSION,
    McpError,
    McpServer,
    McpServerNotFoundError,
    McpToolInfo,
    parse_mcp_tool_id,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.mcp import McpClient, McpToolAdapter, build_transport
from samurai_ai.execution.registry import ToolAdapter
from samurai_ai.storage.mcp import McpServerRepository


class McpService:
    def __init__(self, repository: McpServerRepository) -> None:
        self.repository = repository

    def add(
        self,
        name: str,
        transport: str,
        *,
        command: tuple[str, ...] | None = None,
        url: str | None = None,
        created_by: str = "operator",
    ) -> McpServer:
        server = McpServer(
            name=name,
            transport=transport,
            command=command,
            url=url,
            created_by=created_by,
        )
        self.repository.add(server)
        return server

    def get(self, name: str) -> McpServer:
        server = self.repository.get(name)
        if server is None:
            raise McpServerNotFoundError(f"MCP server not found: {name}")
        return server

    def list_all(self) -> list[McpServer]:
        return self.repository.list_all()

    def set_enabled(self, name: str, enabled: bool) -> McpServer:
        self.get(name)
        self.repository.set_enabled(name, enabled)
        return self.get(name)

    def remove(self, name: str) -> None:
        if not self.repository.delete(name):
            raise McpServerNotFoundError(f"MCP server not found: {name}")
        self.repository.delete_tools_cache(name)

    def health(self, name: str, scope: ScopeContract | None = None) -> dict[str, Any]:
        server = self.get(name)
        return asyncio.run(self._health(server, scope))

    async def _health(
        self, server: McpServer, scope: ScopeContract | None
    ) -> dict[str, Any]:
        client = McpClient(build_transport(server, scope))
        try:
            await client.initialize()
            await client.ping()
        finally:
            await client.close()
        return {
            "server": server.name,
            "transport": server.transport,
            "protocol": MCP_PROTOCOL_VERSION,
            "server_info": client.server_info,
            "ping": True,
        }

    def fetch_tools(
        self, name: str, scope: ScopeContract | None = None
    ) -> list[McpToolInfo]:
        server = self.get(name)
        tools = asyncio.run(self._list_tools(server, scope))
        self.repository.set_tools_cache(name, tools)
        return tools

    async def _list_tools(
        self, server: McpServer, scope: ScopeContract | None
    ) -> list[McpToolInfo]:
        client = McpClient(build_transport(server, scope))
        try:
            await client.initialize()
            return await client.list_tools()
        finally:
            await client.close()

    def cached_tools(self, name: str) -> list[McpToolInfo]:
        self.get(name)
        return self.repository.get_tools_cache(name)

    def resolver(self) -> McpToolResolver:
        return McpToolResolver(self.repository)


class McpToolResolver:
    """Fail-closed dynamic resolver for mcp.<server>.<tool> ids.

    Only servers that are registered, enabled and whose tool schemas were
    explicitly fetched by the operator (`samurai mcp tools <name>`) resolve
    to adapters; anything else returns None and the registry raises
    ToolNotFoundError.
    """

    def __init__(self, repository: McpServerRepository) -> None:
        self.repository = repository

    def resolve(self, tool_id: str) -> ToolAdapter | None:
        try:
            server_name, tool_name = parse_mcp_tool_id(tool_id)
        except McpError:
            return None
        server = self.repository.get(server_name)
        if server is None or not server.enabled:
            return None
        tool = self._find_tool(server_name, tool_name)
        if tool is None:
            return None
        return McpToolAdapter(server, tool)

    def definitions(self) -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for server in self.repository.list_all():
            if not server.enabled:
                continue
            for tool in self.repository.get_tools_cache(server.name):
                definitions.append(McpToolAdapter(server, tool).definition)
        return definitions

    def _find_tool(self, server_name: str, tool_name: str) -> McpToolInfo | None:
        for tool in self.repository.get_tools_cache(server_name):
            if tool.name == tool_name:
                return tool
        return None
