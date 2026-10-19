from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.execution import ToolExecutionError

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CLIENT_NAME = "samurai-ai"
MCP_CLIENT_VERSION = "0.1.0"
MCP_TOOL_ID = re.compile(r"mcp\.([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)\.([A-Za-z0-9_-]{1,128})\Z")
_SERVER_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")

TRUST_UNTRUSTED_EXTERNAL = "untrusted-external"
_ALLOWED_TRANSPORTS = frozenset({"stdio", "http"})


class McpError(ToolExecutionError):
    pass


class McpValidationError(McpError):
    pass


class McpProtocolError(McpError):
    pass


class McpTransportError(McpError):
    pass


class McpServerNotFoundError(LookupError):
    pass


class McpServer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"mcp_{uuid4().hex[:12]}")
    name: str
    transport: str
    command: tuple[str, ...] | None = None
    url: str | None = None
    trust_level: str = TRUST_UNTRUSTED_EXTERNAL
    enabled: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = "operator"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SERVER_NAME.fullmatch(value):
            raise McpValidationError(f"invalid MCP server name: {value!r}")
        return value

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, value: str) -> str:
        if value not in _ALLOWED_TRANSPORTS:
            raise McpValidationError(
                f"MCP transport must be one of {sorted(_ALLOWED_TRANSPORTS)}: {value!r}"
            )
        return value

    @field_validator("trust_level")
    @classmethod
    def validate_trust(cls, value: str) -> str:
        if value != TRUST_UNTRUSTED_EXTERNAL:
            raise McpValidationError(
                f"MCP server trust level must be {TRUST_UNTRUSTED_EXTERNAL!r}: {value!r}"
            )
        return value

    def model_post_init(self, context: Any, /) -> None:
        if self.transport == "stdio" and not self.command:
            raise McpValidationError(f"stdio MCP server {self.name!r} requires a command")
        if self.transport == "stdio" and self.command and any(
            "\x00" in part for part in self.command
        ):
            raise McpValidationError("MCP server command cannot contain NUL bytes")
        if self.transport == "http" and not self.url:
            raise McpValidationError(f"http MCP server {self.name!r} requires a url")


class McpToolInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


def parse_mcp_tool_id(tool_id: str) -> tuple[str, str]:
    match = MCP_TOOL_ID.fullmatch(tool_id)
    if match is None:
        raise McpValidationError(
            f"invalid MCP tool id {tool_id!r}; expected mcp.<server>.<tool>"
        )
    return match.group(1), match.group(2)
