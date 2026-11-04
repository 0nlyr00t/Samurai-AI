"""Questions and approvals over the socket, answered from a second client."""

import asyncio
from typing import Any

from samurai_ai.domain.access import Principal, Role
from samurai_ai.services.rpc.approvals import ApprovalWaiter
from samurai_ai.services.rpc.carriers import QueueCarrier
from samurai_ai.services.rpc.peer import Peer, PeerKind
from samurai_ai.services.rpc.protocol import PROTOCOL_VERSION
from samurai_ai.services.rpc.server import EventFeed, MethodError, RpcServer


class FakeDecisions:
    def __init__(self) -> None:
        self.pending = {"apr_1"}
        self.answered: list[tuple[str, list[Any]]] = []

    def approve(self, approval_id: str) -> None:
        self.pending.discard(approval_id)

    def answer(self, call_id: str, answers: list[Any]) -> None:
        self.answered.append((call_id, answers))


def build(state: FakeDecisions) -> RpcServer:
    server = RpcServer(EventFeed(lambda _o, _s, _l: []))

    async def approvals_list(_peer: Peer, _params: dict[str, Any]) -> list[dict[str, str]]:
        return [{"id": item, "action": "model.call"} for item in sorted(state.pending)]

    async def decide(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        approval_id = str(params["approval_id"])
        if approval_id not in state.pending:
            raise MethodError("unknown", "no such pending approval")
        state.approve(approval_id)
        return {"id": approval_id, "status": "rejected" if params.get("reject") else "approved"}

    async def questions_answer(_peer: Peer, params: dict[str, Any]) -> list[dict[str, str]]:
        data = params.get("answers") or []
        if not data:
            raise MethodError("empty", "an answer needs at least one question")
        state.answer(str(params["call_id"]), data)
        return [{"id": item["id"], "status": "answered"} for item in data]

    server.register("approvals.list", approvals_list, path="operation.show")
    server.register("approvals.decide", decide, write=True, path="agent.answer")
    server.register("questions.answer", questions_answer, write=True, path="agent.answer")
    return server


async def client_for(server: RpcServer, role: Role = Role.OPERATOR):
    carrier = QueueCarrier()
    peer = Peer(principal=Principal(subject="x", role=role), kind=PeerKind.IN_PROCESS)
    task = await carrier.accept(lambda link: server.serve(link, peer=peer))
    await carrier.link.write({"hello": PROTOCOL_VERSION, "client": "t"})
    await carrier.link.read()
    return carrier, task


async def rpc(link, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    await link.write({"id": 1, "method": method, "params": params or {}})
    return await link.read()


def test_a_pending_approval_is_visible_and_decidable() -> None:
    async def scenario() -> None:
        state = FakeDecisions()
        server = build(state)
        carrier, task = await client_for(server)
        listed = await rpc(carrier.link, "approvals.list")
        assert listed["result"] == [{"id": "apr_1", "action": "model.call"}]
        decided = await rpc(carrier.link, "approvals.decide", {"approval_id": "apr_1"})
        assert decided["result"]["status"] == "approved"
        assert state.pending == set()
        await carrier.link.close(), await task, await carrier.stop()

    asyncio.run(scenario())


def test_an_answer_needs_content() -> None:
    async def scenario() -> None:
        state = FakeDecisions()
        server = build(state)
        carrier, task = await client_for(server)
        refused = await rpc(carrier.link, "questions.answer", {"call_id": "q1"})
        assert refused["error"]["code"] == "empty"
        done = await rpc(
            carrier.link,
            "questions.answer",
            {"call_id": "q1", "answers": [{"id": "target", "selected": ["app"]}]},
        )
        assert done["result"][0]["status"] == "answered"
        assert state.answered[0][0] == "q1"
        await carrier.link.close(), await task, await carrier.stop()

    asyncio.run(scenario())


def test_a_viewer_may_list_but_not_decide() -> None:
    async def scenario() -> None:
        state = FakeDecisions()
        server = build(state)
        carrier, task = await client_for(server, role=Role.VIEWER)
        assert (await rpc(carrier.link, "approvals.list"))["result"]
        assert (await rpc(carrier.link, "approvals.decide", {"approval_id": "apr_1"}))[
            "error"
        ]["code"] == "forbidden"
        await carrier.link.close(), await task, await carrier.stop()

    asyncio.run(scenario())


def test_the_waiter_returns_once_someone_decides() -> None:
    state = FakeDecisions()
    waiter = ApprovalWaiter(
        lambda approval_id: approval_id in state.pending, wait_seconds=2, poll_seconds=0.02
    )

    def decide_soon() -> None:
        import threading

        threading.Timer(0.1, state.approve, args=("apr_1",)).start()

    decide_soon()
    assert waiter("apr_1") is True


def test_the_waiter_gives_up_after_its_deadline() -> None:
    waiter = ApprovalWaiter(lambda _id: True, wait_seconds=0.1, poll_seconds=0.02)
    assert waiter("apr_1") is False
