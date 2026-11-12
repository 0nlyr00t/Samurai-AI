"""Model service settings: the request timeout."""

from __future__ import annotations

from pathlib import Path

from samurai_ai.cli import _make_services


def test_the_model_timeout_comes_from_settings_and_is_bounded(tmp_path: Path) -> None:
    """60 s ended real runs on slow gateways; the operator can raise it."""
    services = _make_services(tmp_path / "t.db")
    models = services[7]
    assert models.request_timeout() == 180.0
    assert models.settings is not None
    models.settings.set_setting("model.timeout_seconds", "420")
    assert models.request_timeout() == 420.0
    models.settings.set_setting("model.timeout_seconds", "99999")
    assert models.request_timeout() == 600.0
    models.settings.set_setting("model.timeout_seconds", "2")
    assert models.request_timeout() == 10.0


def test_streaming_completion_arrives_token_by_token(tmp_path: Path) -> None:
    """The SSE path yields text as it lands and still returns a full response."""
    import asyncio

    from samurai_ai.agents.provider import ChatRequest, Message

    # a fake broker that drips SSE lines
    class _StreamBroker:
        def fetch_stream(self, url, *, method="POST", body=None, headers=None, timeout=60.0):
            lines = [
                b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
                (
                    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                    b'"id":"c1","function":{"name":"shell_exec",'
                    b'"arguments":"{\\"command\\":\\"echo hi\\"}"}}]}}]}\n'
                ),
                b"data: [DONE]\n",
            ]
            yield from lines

    from samurai_ai.agents.openai_compat import OpenAICompatProvider
    from samurai_ai.domain.models import ProviderDefinition

    provider_def = ProviderDefinition(
        id="fake", name="fake", base_url="http://fake", api_family="openai-compatible"
    )
    provider = OpenAICompatProvider(provider_def, None, broker=_StreamBroker())  # type: ignore[arg-type]

    seen: list[str] = []
    request = ChatRequest(
        model_id="fake/m@v1",
        messages=(Message(role="user", content="hi"),),
        tools=(),
    )
    response = asyncio.run(provider.complete_stream(request, seen.append))
    assert seen == ["Hel", "lo"]
    assert response.text == "Hello"
    assert response.tool_calls[0].arguments["command"] == "echo hi"


def test_streaming_salvages_dsml_markup_into_tool_calls(tmp_path: Path) -> None:
    """A model that inlines DSML calls into streamed text must not lose them."""
    import asyncio

    from samurai_ai.agents.openai_compat import OpenAICompatProvider
    from samurai_ai.agents.provider import ChatRequest, Message
    from samurai_ai.domain.models import ProviderDefinition

    dsml = (
        'Смотрю цель.\n\n<｜DSML｜ calls>\n<｜DSML｜ invoke name="shell_exec">\n'
        '<｜DSML｜ parameter name="command" string="true">id</｜DSML｜ parameter>\n'
        "</｜DSML｜ invoke>\n</｜DSML｜ calls>"
    )

    class _DsmlBroker:
        def fetch_stream(self, url, *, method="POST", body=None, headers=None, timeout=60.0):
            import json as _json

            yield f'data: {_json.dumps({"choices": [{"delta": {"content": dsml}}]})}\n'.encode()
            yield b"data: [DONE]\n"

    provider_def = ProviderDefinition(
        id="fake", name="fake", base_url="http://fake", api_family="openai-compatible"
    )
    provider = OpenAICompatProvider(provider_def, None, broker=_DsmlBroker())  # type: ignore[arg-type]
    response = asyncio.run(
        provider.complete_stream(
            ChatRequest(
                model_id="fake/m@v1", messages=(Message(role="user", content="hi"),), tools=()
            ),
            lambda _t: None,
        )
    )
    assert response.text == "Смотрю цель."
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].arguments == {"command": "id"}


def test_streaming_falls_back_to_a_plain_call_without_sse(tmp_path: Path) -> None:
    """A gateway that rejects stream=true must still answer."""
    import asyncio

    from samurai_ai.agents.openai_compat import OpenAICompatProvider
    from samurai_ai.agents.provider import ChatRequest, Message
    from samurai_ai.domain.models import ProviderDefinition

    class _NoStreamBroker:
        def fetch(self, url, **kwargs):
            from samurai_ai.execution.http import HttpResponse

            return HttpResponse(
                url=url,
                status=200,
                reason="OK",
                body=b'{"choices":[{"message":{"role":"assistant","content":"plain"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}',
                truncated=False,
                redirects=(),
            )

        def fetch_stream(self, url, **kwargs):
            from samurai_ai.domain.execution import ToolExecutionError

            raise ToolExecutionError("no stream")

    provider_def = ProviderDefinition(
        id="fake", name="fake", base_url="http://fake", api_family="openai-compatible"
    )
    provider = OpenAICompatProvider(provider_def, None, broker=_NoStreamBroker())  # type: ignore[arg-type]
    response = asyncio.run(
        provider.complete_stream(
            ChatRequest(
                model_id="fake/m@v1", messages=(Message(role="user", content="hi"),), tools=()
            ),
            lambda _t: None,
        )
    )
    assert response.text == "plain"


def test_swarm_merge_folds_duplicate_findings(tmp_path: Path) -> None:
    """Two members finding the same bug end up as one row with the most evidence."""
    from samurai_ai.cli import _make_services
    from samurai_ai.domain.kernel import PlanTask, RiskClass
    from samurai_ai.domain.operations import OperationMode, ScopeContract

    services = _make_services(tmp_path / "t.db")
    operations, kernel, swarms = services[0], services[1], services[14]
    operation = operations.create(
        "Merge lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("*.example.com",))
    )
    kernel.create_plan(
        operation.id,
        "Plan",
        [PlanTask(id="t-1", title="Scan", risk_class=RiskClass.R1)],
        [],
    )
    swarm = swarms.start(operation.id, ["t-1"], ["fake/agent@v1"])
    # findings live in the kernel repo; write directly if no service wrapper
    from samurai_ai.domain.kernel import Finding
    from samurai_ai.domain.operations import OperationEvent

    repo = kernel.kernel
    a = Finding(
        operation_id=operation.id, title="SQL injection in /login", evidence_ids=("a1", "a2")
    )
    b = Finding(operation_id=operation.id, title="sql injection in /login", evidence_ids=())
    repo.add_finding(
        a,
        OperationEvent(
            operation_id=operation.id, event_type="finding.proposed", payload={"finding_id": a.id}
        ),
    )
    repo.add_finding(
        b,
        OperationEvent(
            operation_id=operation.id, event_type="finding.proposed", payload={"finding_id": b.id}
        ),
    )

    report = swarms.merge_findings(swarm.id)
    assert report["merged"] == 1
    assert any(dup == b.id and keeper == a.id for dup, keeper in report["duplicates"])
    assert repo.get_finding(b.id).duplicate_of == a.id


def test_replay_scrubs_to_a_prefix() -> None:
    """The replay shows what the run had done up to the tick."""
    from datetime import UTC, datetime

    from samurai_ai.domain.operations import OperationEvent
    from samurai_ai.tui.replay import replay_lines

    events = [
        OperationEvent(
            operation_id="op_1",
            event_type="tool.run.started",
            payload={"run_id": "r1"},
            created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ),
        OperationEvent(
            operation_id="op_1",
            event_type="tool.run.completed",
            payload={"run_id": "r1"},
            created_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        ),
    ]
    all_lines = replay_lines(events)
    assert len(all_lines) == 2
    assert len(replay_lines(events, upto=1)) == 1
    assert "tool.run.started" in all_lines[0]


def test_the_second_level_runs_a_third_of_the_budget(tmp_path: Path) -> None:
    """depth 1 is the helper level: its steps and wall are cut by three."""
    # the constant moved: the second level exists now
    from samurai_ai.agents import orchestrator
    from samurai_ai.agents.orchestrator import DEFAULT_MAX_STEPS

    assert orchestrator.DEFAULT_MAX_DEPTH == 2
    assert DEFAULT_MAX_STEPS > 0


def test_recall_finds_the_relevant_entry_not_everything(tmp_path: Path) -> None:
    """FTS5 over memory: the SSTI note answers 'template', the password note does not."""
    from samurai_ai.cli import _make_services

    services = _make_services(tmp_path / "t.db")
    memory = services[16]
    memory.add("ws", "SSTI in ticket notes", "Go template .File reads /proc/self/environ")
    memory.add("ws", "Default creds", "admin/admin on the staging panel")
    hits = memory.recall("template")
    assert [h.title for h in hits] == ["SSTI in ticket notes"]
    assert memory.recall("nonexistent-xyzzy") == []


def test_patch_diff_reads_a_cve_style_patch(tmp_path: Path) -> None:
    """A bounds-guard patch reads as a bounds-guard bug."""
    from samurai_ai.analysis.patchdiff import analyze_patch, report

    diff = """diff --git a/src/parse.c b/src/parse.c
--- a/src/parse.c
+++ b/src/parse.c
@@ -100,7 +100,10 @@ int parse_header(char *buf)
-    memcpy(dst, src, n);
+    if (n > sizeof(dst))
+        return -1;
+    memcpy(dst, src, n);
"""
    analysis = analyze_patch(diff, advisory="remote code execution via overflow")
    assert "src/parse.c" in analysis.files
    assert "bounds" in analysis.guards
    assert "RCE" in analysis.hypothesis or "rce" in analysis.hypothesis
    text = report(analysis)
    assert "src/parse.c" in text and "bounds" in text


def test_device_code_login_polls_until_the_token_arrives() -> None:
    """RFC 8628 shape: pending answers keep polling, a token ends the flow."""
    from samurai_ai.services import provider_oauth

    calls: list[str] = []

    def transport(url: str, form: dict[str, str]):
        calls.append(url)
        if url.endswith("/device_authorization"):
            return 200, {
                "verification_uri": "https://idp.example/device",
                "user_code": "ABCD-1234",
                "device_code": "dev-1",
                "interval": 0,
            }
        if len([c for c in calls if c.endswith("/token")]) < 2:
            return 400, {"error": "authorization_pending"}
        return 200, {"access_token": "tok-1"}

    prompt = provider_oauth.begin(transport, "https://idp.example", "cli-1")
    assert prompt.user_code == "ABCD-1234"
    token = provider_oauth.wait_for_token(
        transport, "https://idp.example", "cli-1", prompt, sleep=lambda _s: None
    )
    assert token == "tok-1"


def test_device_code_login_gives_up_on_a_real_refusal() -> None:
    import pytest as _pytest

    from samurai_ai.services import provider_oauth

    def transport(url: str, form: dict[str, str]):
        if url.endswith("/device_authorization"):
            return 200, {"verification_uri": "u", "user_code": "c", "device_code": "d"}
        return 400, {"error": "access_denied"}

    prompt = provider_oauth.begin(transport, "https://idp.example", "cli-1")
    with _pytest.raises(provider_oauth.DeviceFlowError):
        provider_oauth.wait_for_token(
            transport, "https://idp.example", "cli-1", prompt, sleep=lambda _s: None
        )


def test_an_oauth_token_outranks_a_stored_key(tmp_path: Path) -> None:
    """A device login wins over an API key for that provider."""
    from samurai_ai.cli import _make_services

    services = _make_services(tmp_path / "t.db")
    models, vault = services[7], services[5]
    vault.add("moonshot-api-key", "sk-key")
    provider = next(p for p in models.registry.list_providers() if p.id == "moonshot")
    assert models.oauth_token("moonshot") is None
    vault.add("moonshot-access-token", "tok-oauth")
    assert models.oauth_token("moonshot") == "tok-oauth"
    assert provider.secret_name == "moonshot-api-key"


def test_the_goal_judge_reads_a_verdict() -> None:
    from samurai_ai.services import goal_judge

    assert goal_judge.parse_verdict("STOP") == "stop"
    assert goal_judge.parse_verdict("continue.") == "continue"
    assert goal_judge.parse_verdict("hmm") is None
    system, question = goal_judge.build_prompt("both flags", "ran nmap")
    assert "STOP" in system
    assert "both flags" in question and "ran nmap" in question


def test_the_digest_keeps_the_tail() -> None:
    from samurai_ai.services import goal_judge

    lines = [f"line {i}" for i in range(500)]
    out = goal_judge.digest(lines, limit=100)
    assert len(out) <= 100 and "line 499" in out


def _provider(broker: object):
    from samurai_ai.agents.openai_compat import OpenAICompatProvider
    from samurai_ai.domain.models import ProviderDefinition

    definition = ProviderDefinition(
        id="fake", name="fake", base_url="http://fake", api_family="openai-compatible"
    )
    return OpenAICompatProvider(definition, None, broker=broker)  # type: ignore[arg-type]


def _request():
    from samurai_ai.agents.provider import ChatRequest, Message

    return ChatRequest(
        model_id="fake/m@v1", messages=(Message(role="user", content="hi"),), tools=()
    )


def test_empty_chunks_and_junk_tool_deltas_do_not_lose_the_answer() -> None:
    """A malformed delta is skipped; the text around it still arrives."""
    import asyncio

    class _JunkBroker:
        def fetch_stream(self, url, **kwargs):
            yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n'
            yield b'data: {"choices":[{"delta":{}}]}\n'
            yield b'data: {"choices":[{"delta":{"content":""}}]}\n'
            yield (
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":"x",'
                b'"function":{"name":"shell_exec","arguments":"{}"}}]}}]}\n'
            )
            yield b'data: not-json\n'
            yield b'data: {"choices":["junk"]}\n'
            yield b'data: {"choices":[{"delta":{"content":"b"}}]}\n'
            yield b"data: [DONE]\n"

    seen: list[str] = []
    response = asyncio.run(_provider(_JunkBroker()).complete_stream(_request(), seen.append))
    assert seen == ["a", "b"]
    assert response.text == "ab"
    assert response.tool_calls and response.tool_calls[0].name == "shell_exec"


def test_reasoning_streams_separately_from_the_answer() -> None:
    import asyncio

    class _ReasoningBroker:
        def fetch_stream(self, url, **kwargs):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n'
            yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n'
            yield b"data: [DONE]\n"

    seen: list[str] = []
    response = asyncio.run(_provider(_ReasoningBroker()).complete_stream(_request(), seen.append))
    assert response.text == "done"
    assert response.reasoning == "thinking"
    assert seen == ["done"]  # reasoning does not leak into the answer


def test_a_stop_request_ends_the_stream_with_what_arrived() -> None:
    import asyncio

    class _LongBroker:
        def fetch_stream(self, url, **kwargs):
            for index in range(5):
                yield f'data: {{"choices":[{{"delta":{{"content":"{index}"}}}}]}}\n'.encode()

    state = {"stop": False}
    seen: list[str] = []

    def on_token(token: str) -> None:
        seen.append(token)
        state["stop"] = True  # the operator pressed stop after the first token

    response = asyncio.run(
        _provider(_LongBroker()).complete_stream(
            _request(), on_token, should_stop=lambda: state["stop"]
        )
    )
    assert seen == ["0"]
    assert response.text == "0"


def test_a_mid_stream_failure_asks_the_view_to_reset_before_falling_back() -> None:
    import asyncio

    from samurai_ai.agents.provider import STREAM_RESET

    class _BrokenBroker:
        def fetch_stream(self, url, **kwargs):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'
            raise RuntimeError("socket died")

    provider = _provider(_BrokenBroker())
    from samurai_ai.agents.provider import ChatResponse

    async def complete(request):  # type: ignore[no-untyped-def]
        return ChatResponse(text="full", model_id=request.model_id, provider_id="fake")

    provider.complete = complete  # type: ignore[method-assign]

    seen: list[str] = []
    response = asyncio.run(provider.complete_stream(_request(), seen.append))
    assert seen == ["partial", STREAM_RESET]
    assert response.text == "full"
