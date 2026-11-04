"""`samurai serve` and the clients that attach to it.

The daemon owns the session; a client is a viewer or a writer, never the owner.
Losing a client is losing a window, not losing the work.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from samurai_ai.config import serve_pidfile_path, serve_socket_path
from samurai_ai.services.rpc.server import RpcServer

Command = Callable[[argparse.Namespace], int]


def add_serve_commands(subcommands: Any, add_parser: Callable[..., Any]) -> None:
    serve = add_parser("serve", help="Run the session daemon on a unix socket")
    serve.add_argument("--socket", default=argparse.SUPPRESS, help="socket path override")
    serve.add_argument("--ready-file", default=None, help="print the socket path here")
    serve.set_defaults(handler=_serve)

    attach = add_parser("attach", help="Follow an operation's event stream")
    attach.add_argument("operation_id")
    attach.add_argument("--from", dest="from_seq", type=int, default=0)
    attach.add_argument("--socket", default=argparse.SUPPRESS)
    attach.add_argument("--once", action="store_true", help="print the backlog and exit")
    attach.set_defaults(handler=_attach)

    send = add_parser("send", help="Write into a running session")
    send.add_argument("operation_id")
    what = send.add_mutually_exclusive_group(required=True)
    what.add_argument("--message", help="a note to the journal")
    what.add_argument("--approve", nargs="?", const="", help="approve the pending step")
    what.add_argument("--reject", nargs="?", const="", help="decline the pending step")
    what.add_argument("--answer", metavar="CALL_ID", help="answer an agent's questions")
    send.add_argument("--pick", action="append", default=[], metavar="ID=LABEL")
    send.add_argument("--custom", action="append", default=[], metavar="ID=TEXT")
    send.add_argument("--socket", default=argparse.SUPPRESS)
    send.set_defaults(handler=_send)

    call = add_parser("call", help="Call a session method with JSON params")
    call.add_argument("method")
    call.add_argument("--params", default="{}")
    call.add_argument("--socket", default=argparse.SUPPRESS)
    call.add_argument("--read-only", action="store_true")
    call.set_defaults(handler=_call)


def _socket_of(args: argparse.Namespace) -> Path:
    """`--socket` works both before and after the command."""
    given = getattr(args, "socket", None)
    return Path(given) if given else serve_socket_path()


# -- daemon ---------------------------------------------------------------
def _build_methods(database_path: Path | None) -> tuple[RpcServer, Any]:
    from samurai_ai.cli import _make_services
    from samurai_ai.domain.kernel import ApprovalStatus
    from samurai_ai.domain.operations import OperationEvent
    from samurai_ai.services.rpc.approvals import ApprovalWaiter
    from samurai_ai.services.rpc.peer import Peer
    from samurai_ai.services.rpc.runs import RunBusy, RunHandle, RunRegistry
    from samurai_ai.services.rpc.server import EventFeed, MethodError, RpcServer

    services = _make_services(database_path)
    operations = services[0]
    repository = operations.repository
    swarm_service = services[14]
    questions_service = services[21]
    kernel_service = services[1]

    def journal(operation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        from samurai_ai.domain.operations import OperationEvent

        repository.append_event(
            OperationEvent(
                operation_id=operation_id, event_type=event_type, payload=payload
            )
        )

    def still_pending(approval_id: str) -> bool:
        """Read the approval fresh: another process may have decided it."""
        approval = kernel_service.kernel.get_approval(approval_id)
        return approval is None or approval.status is ApprovalStatus.PENDING

    waiter = ApprovalWaiter(still_pending)

    async def work(handle: RunHandle) -> None:
        """Work the plan: each swarm only reaches the tasks ready when it starts."""
        while not handle.stop_event.is_set():
            candidates = swarm_service.ready_tasks(handle.operation_id)
            ready = [
                task
                for task in candidates
                if handle.task_ids is None or task.id in set(handle.task_ids)
            ]
            if not ready:
                return
            await asyncio.to_thread(
                swarm_service.start,
                handle.operation_id,
                [task.id for task in ready],
                handle.route,
                created_by=handle.created_by,
                approval_resolver=waiter,
                stop_event=handle.stop_event,
            )

    runs = RunRegistry(work, journal)

    async def operation_list(_peer: Peer, _params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"id": operation.id, "name": operation.name, "status": operation.status.value}
            for operation in operations.list_all()
        ]

    async def operation_show(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        operation = operations.get(str(params.get("operation_id") or ""))
        return operation.model_dump(mode="json")

    async def events_subscribe(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        operation = operations.get(str(params.get("operation_id") or ""))
        return {"operation_id": operation.id, "from_seq": int(params.get("from_seq") or 0)}

    async def send_message(peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("message") or "").strip()
        if not text:
            raise MethodError("empty", "a message needs text")
        operation = operations.get(str(params.get("operation_id") or ""))
        repository.append_event(
            OperationEvent(
                operation_id=operation.id,
                event_type="operator.message",
                payload={"text": text, "peer": peer.label},
            )
        )
        return {"operation_id": operation.id, "text": text}

    async def run_start(peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        operation = operations.get(str(params.get("operation_id") or ""))
        route = [str(params.get("model_id") or "")]
        if not route[0]:
            raise MethodError("missing", "model_id is required")
        task_ids = params.get("task_ids")
        try:
            handle = runs.start(
                operation.id,
                route,
                [str(task) for task in task_ids] if task_ids else None,
                peer.label,
            )
        except RunBusy as exc:
            raise MethodError("busy", str(exc)) from exc
        return handle.as_frame()

    async def run_list(_peer: Peer, params: dict[str, Any]) -> list[dict[str, Any]]:
        return runs.listing(params.get("operation_id"))

    async def run_cancel(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        try:
            handle = runs.cancel(str(params.get("run_id") or ""))
        except KeyError as exc:
            raise MethodError("unknown-run", "no such run") from exc
        return handle.as_frame()

    async def approvals_list(_peer: Peer, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows = kernel_service.kernel.list_approvals(
            str(params.get("operation_id") or ""), status=ApprovalStatus.PENDING
        )
        return [
            {"id": row.id, "action": row.action, "risk": row.risk_class.value}
            for row in rows
        ]

    async def approvals_decide(peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        approval_id = str(params.get("approval_id") or "")
        if params.get("reject"):
            return kernel_service.reject(approval_id, peer.label).model_dump(mode="json")
        return kernel_service.approve(approval_id, peer.label).model_dump(mode="json")

    async def questions_list(_peer: Peer, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows = questions_service.pending(str(params.get("operation_id") or ""))
        return [
            {"call_id": row.call_id, "id": row.id, "payload": json.loads(row.payload)}
            for row in rows
        ]

    async def questions_answer(peer: Peer, params: dict[str, Any]) -> list[dict[str, Any]]:
        from samurai_ai.services.questions import Answer

        answers = [
            Answer(
                id=str(item.get("id")),
                selected=tuple(item.get("selected") or ()),
                custom=str(item.get("custom") or ""),
            )
            for item in params.get("answers") or []
        ]
        if not answers:
            raise MethodError("empty", "an answer needs at least one question")
        rows = questions_service.answer(
            str(params.get("call_id") or ""), answers, peer.label
        )
        return [{"id": row.id, "status": row.status} for row in rows]

    server = RpcServer(EventFeed(repository.events_since))
    server.register("approvals.list", approvals_list)
    server.register("approvals.decide", approvals_decide, write=True)
    server.register("questions.list", questions_list)
    server.register("questions.answer", questions_answer, write=True)
    server.register("run.start", run_start, write=True)
    server.register("run.list", run_list)
    server.register("run.cancel", run_cancel, write=True)
    server.register("operation.list", operation_list)
    server.register("operation.show", operation_show)
    server.register("events.subscribe", events_subscribe)
    server.register("send.message", send_message, write=True)
    return server, runs


async def _run_daemon(args: argparse.Namespace) -> int:
    from samurai_ai.services.rpc.carriers import UnixSocketCarrier

    socket_path = _socket_of(args)
    server, runs = _build_methods(getattr(args, "db", None))
    carrier = UnixSocketCarrier(socket_path)
    await server.start()
    await carrier.start(server.serve)
    pidfile = serve_pidfile_path()
    await asyncio.to_thread(pidfile.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(pidfile.write_text, str(os.getpid()))
    if getattr(args, "ready_file", None):
        await asyncio.to_thread(Path(args.ready_file).write_text, str(socket_path))
    print(f"serving on {socket_path}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with_suppress = loop
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # pragma: no cover - non-unix
            with_suppress.call_soon_threadsafe(stop.set)
    await stop.wait()
    await carrier.stop()
    await runs.drain()
    await server.aclose()
    pidfile.unlink(missing_ok=True)
    return 0


def _serve(args: argparse.Namespace) -> int:
    return asyncio.run(_run_daemon(args))


# -- clients --------------------------------------------------------------
async def _attach_async(args: argparse.Namespace) -> int:
    from samurai_ai.services.rpc.client import Client, RpcFailure, format_event

    try:
        client = await Client.connect(_socket_of(args), client="attach", read_only=True)
    except RpcFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        await client.subscribe(args.operation_id, args.from_seq)
        if not args.once:
            async for frame in client.stream():
                print(format_event(frame), flush=True)
            return 0
        # `--once`: print the backlog, then leave when the stream goes quiet
        while True:
            try:
                frame = await asyncio.wait_for(client.events.get(), 0.5)
            except TimeoutError:
                return 0
            if frame is None:
                return 0
            print(format_event(frame), flush=True)
    finally:
        await client.close()


def _attach(args: argparse.Namespace) -> int:
    return asyncio.run(_attach_async(args))


async def _call_async(
    args: argparse.Namespace, *, read_only: bool
) -> tuple[int, Any]:
    from samurai_ai.services.rpc.client import Client, RpcFailure

    try:
        client = await Client.connect(_socket_of(args), client="cli", read_only=read_only)
    except RpcFailure as exc:
        return 1, str(exc)
    try:
        return 0, await client.call(args.method, json.loads(args.params or "{}"))
    except RpcFailure as exc:
        return 1, str(exc)
    finally:
        await client.close()


async def _send_call(client: Any, args: argparse.Namespace) -> str:
    """One connection, one write: the shape the daemon expects per action."""
    if args.message is not None:
        await client.call(
            "send.message", {"operation_id": args.operation_id, "message": args.message}
        )
        return "sent"
    if args.approve is not None or args.reject is not None:
        reject = args.reject is not None
        approval_id = args.reject if reject else args.approve
        if not approval_id:
            listing = await client.call(
                "approvals.list", {"operation_id": args.operation_id}
            )
            if not listing:
                raise LookupError("nothing is pending")
            approval_id = listing[0]["id"]
        await client.call(
            "approvals.decide", {"approval_id": approval_id, "reject": reject}
        )
        return f"{'rejected' if reject else 'approved'} {approval_id}"
    answers: dict[str, dict[str, Any]] = {}
    for chunk in args.pick:
        ident, _, label = chunk.partition("=")
        answers.setdefault(ident, {"id": ident, "selected": []})["selected"].append(label)
    for chunk in args.custom:
        ident, _, text = chunk.partition("=")
        answers.setdefault(ident, {"id": ident, "selected": []})["custom"] = text
    rows = await client.call(
        "questions.answer", {"call_id": args.answer, "answers": list(answers.values())}
    )
    return f"answered {len(rows)} question(s)"


def _send(args: argparse.Namespace) -> int:
    async def run() -> int:
        from samurai_ai.services.rpc.client import Client, RpcFailure

        try:
            client = await Client.connect(_socket_of(args), client="send")
        except RpcFailure as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            print(await _send_call(client, args))
            return 0
        except (RpcFailure, LookupError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            await client.close()

    return asyncio.run(run())


def _call(args: argparse.Namespace) -> int:
    code, result = asyncio.run(_call_async(args, read_only=args.read_only))
    if code:
        print(f"error: {result}", file=sys.stderr)
        return code
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


__all__ = ["add_serve_commands"]
