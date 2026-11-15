"""Governed adapter for stateful session control."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from samurai_ai.domain.execution import (
    NetworkAccess,
    ToolDefinition,
    ToolKind,
    ToolValidationError,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.registry import ToolOutput
from samurai_ai.services.sessions import CommandStatus, Session, SessionCommand, SessionService


class SessionControlTool:
    def __init__(
        self,
        *,
        manager: ProcessManager | None = None,
        autonomy_required: bool = False,
    ) -> None:
        self.sessions = SessionService(
            manager=manager,
            autonomy_required=autonomy_required,
        )
        self.autonomy_required = autonomy_required
        self._definition = ToolDefinition(
            id="session.control",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Open and control a stateful SSH/PTY session: open, exec, "
            "read, write, wait, cancel, retry, reconnect and close. Unknown "
            "remote outcomes are never replayed automatically.",
            risk_class=RiskClass.R3,
            execution_mode="sequential",
            network=NetworkAccess.DIRECT,
            timeout_seconds=300,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "open",
                            "exec",
                            "read",
                            "write",
                            "wait",
                            "cancel",
                            "retry",
                            "reconnect",
                            "healthcheck",
                            "close",
                        ],
                    },
                    "host": {"type": "string", "minLength": 1, "maxLength": 253},
                    "user": {"type": "string", "minLength": 1, "maxLength": 64},
                    "port": {"type": "integer", "default": 22},
                    "password": {"type": "string", "maxLength": 1024},
                    "key": {"type": "string"},
                    "session_id": {"type": "string"},
                    "command_id": {"type": "string"},
                    "command": {"type": "string", "maxLength": 4000},
                    "pty": {"type": "boolean", "default": False},
                    "wait_seconds": {"type": "number", "minimum": 0},
                    "limit": {"type": "integer", "default": 4000, "minimum": 1},
                    "data": {"type": "string", "maxLength": 65536},
                    "eof": {"type": "boolean", "default": False},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            capabilities=("remote", "admin", "ssh", "pty", "sessions"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def autonomy_required(self) -> bool:
        return self.sessions.autonomy_required

    @autonomy_required.setter
    def autonomy_required(self, value: bool) -> None:
        self.sessions.autonomy_required = value

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        return asyncio.run(self._invoke(params, scope))

    async def _invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        action = str(params["action"])
        unknown = False
        if action == "open":
            session = await self.sessions.open(
                str(params.get("_operation_id", "")),
                scope,
                self._required(params, "host"),
                self._required(params, "user"),
                password=params.get("password"),
                key=params.get("key"),
                port=int(params.get("port") or 22),
            )
            payload: dict[str, Any] = self._session_json(session)
        elif action == "exec":
            command = await self.sessions.exec(
                self._required(params, "session_id"),
                self._required(params, "command"),
                pty=bool(params.get("pty", False)),
                wait_seconds=params.get("wait_seconds"),
            )
            payload = self._command_json(command)
            unknown = command.status is CommandStatus.UNKNOWN
        elif action == "read":
            output = await self.sessions.read(
                self._required(params, "session_id"),
                self._required(params, "command_id"),
                limit=int(params.get("limit") or 4000),
            )
            payload = {"output": output}
        elif action == "write":
            await self.sessions.write(
                self._required(params, "session_id"),
                self._required(params, "command_id"),
                str(params.get("data", "")).encode(),
                eof=bool(params.get("eof", False)),
            )
            payload = {"status": "written"}
        elif action in ("wait", "cancel", "retry"):
            method = getattr(self.sessions, action)
            command = await method(
                self._required(params, "session_id"),
                self._required(params, "command_id"),
                **({"timeout_seconds": params.get("wait_seconds")} if action == "wait" else {}),
            )
            payload = self._command_json(command)
            unknown = command.status is CommandStatus.UNKNOWN
        elif action == "reconnect":
            session = await self.sessions.reconnect(self._required(params, "session_id"))
            payload = self._session_json(session)
        elif action == "healthcheck":
            payload = await self.sessions.healthcheck(
                self._required(params, "session_id")
            )
        elif action == "close":
            await self.sessions.close(self._required(params, "session_id"))
            payload = {"status": "closed"}
        else:
            raise ToolValidationError(f"unsupported session action {action!r}")
        return ToolOutput(
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
            exit_code=0,
            unknown=unknown,
        )

    def _required(self, params: dict[str, Any], name: str) -> str:
        value = str(params.get(name, "")).strip()
        if not value:
            raise ToolValidationError(f"session.{params['action']} requires {name}")
        return value

    def _session_json(self, session: Session) -> dict[str, Any]:
        return {
            "session_id": session.id,
            "status": session.status.value,
            "host": session.host,
            "user": session.user,
            "port": session.port,
        }

    def _command_json(self, command: SessionCommand) -> dict[str, Any]:
        return {
            "command_id": command.id,
            "session_id": command.session_id,
            "process_id": command.process_id,
            "status": command.status.value,
            "external_outcome": command.external_outcome.value,
            "exit_code": command.exit_code,
            "stdout": command.stdout,
            "stderr": command.stderr,
            "retry_allowed": (
                command.status is not CommandStatus.UNKNOWN
                and command.external_outcome.value == "observed"
            ),
        }
