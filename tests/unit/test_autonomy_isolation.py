import asyncio
from types import SimpleNamespace

import pytest

from samurai_ai.domain.execution import ToolExecutionError
from samurai_ai.execution import remote
from samurai_ai.execution.remote import SshExecTool


def test_remote_driver_refuses_unconfined_autonomous_fallback(monkeypatch) -> None:
    monkeypatch.setattr(remote, "bwrap_available", lambda: False)
    tool = SshExecTool(runner=object())  # type: ignore[arg-type]
    tool.autonomy_required = True
    profile = SimpleNamespace(stall_seconds=1.0, timeout_seconds=1.0)

    with pytest.raises(ToolExecutionError, match="unconfined remote driver fallback is refused"):
        asyncio.run(tool._run_native(profile, ["-c"], {}, {}))
