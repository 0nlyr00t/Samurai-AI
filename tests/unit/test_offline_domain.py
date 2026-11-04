"""What counts as local, and what the offline mode refuses."""

import pytest

from samurai_ai.domain.execution import NetworkAccess, ToolDefinition, ToolKind
from samurai_ai.domain.offline import OfflineError, check_model, check_tool, is_local_url


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:11434", "http://localhost:11434/v1", "https://box.local/v1",
     "http://host.docker.internal:1234", "http://gpu.internal:8000"],
)
def test_local_endpoints_are_local(url: str) -> None:
    assert is_local_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://api.openai.com/v1", "https://api.deepseek.com", "http://192.168.1.9:11434"],
)
def test_the_public_api_is_not_local(url: str) -> None:
    assert not is_local_url(url)


def tool(tool_id: str) -> ToolDefinition:
    return ToolDefinition(
        id=tool_id, title=tool_id, kind=ToolKind.SANDBOX, version=1,
        risk_class="r2", network=NetworkAccess.PROXIED,
    )


@pytest.mark.parametrize(
    "tool_id",
    ["recon.subfinder-passive", "recon.dnsx-resolve", "research.read", "research.search"],
)
def test_outside_tools_are_refused_offline(tool_id: str) -> None:
    with pytest.raises(OfflineError, match="needs the internet"):
        check_tool(tool(tool_id), True)


def test_local_tools_pass_offline() -> None:
    check_tool(tool("scan.nmap"), True)
    check_tool(tool("fuzz.ffuf"), True)


def test_nothing_is_refused_when_the_operation_is_online() -> None:
    check_tool(tool("recon.subfinder-passive"), False)
    check_model("openai/gpt-4o@v1", "https://api.openai.com/v1", False)


def test_a_remote_model_is_refused_offline() -> None:
    with pytest.raises(OfflineError, match="is remote"):
        check_model("openai/gpt-4o@v1", "https://api.openai.com/v1", True)


def test_a_local_model_passes_offline() -> None:
    check_model("ollama/llama3@v1", "http://localhost:11434", True)
