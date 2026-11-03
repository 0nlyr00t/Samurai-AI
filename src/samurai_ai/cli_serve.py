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
    send.add_argument("--message", required=True)
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
def _build_methods(database_path: Path | None) -> RpcServer:
    from samurai_ai.cli import _make_services
    from samurai_ai.domain.operations import OperationEvent
    from samurai_ai.services.rpc.peer import Peer
    from samurai_ai.services.rpc.server import EventFeed, MethodError, RpcServer

    services = _make_services(database_path)
    operations = services[0]
    repository = operations.repository

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

    server = RpcServer(EventFeed(repository.events_since))
    server.register("operation.list", operation_list)
    server.register("operation.show", operation_show)
    server.register("events.subscribe", events_subscribe)
    server.register("send.message", send_message, write=True)
    return server


async def _run_daemon(args: argparse.Namespace) -> int:
    from samurai_ai.services.rpc.carriers import UnixSocketCarrier

    socket_path = _socket_of(args)
    server = _build_methods(getattr(args, "db", None))
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


def _send(args: argparse.Namespace) -> int:
    async def run() -> int:
        from samurai_ai.services.rpc.client import Client, RpcFailure

        try:
            client = await Client.connect(_socket_of(args), client="send")
        except RpcFailure as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            await client.call(
                "send.message",
                {"operation_id": args.operation_id, "message": args.message},
            )
            print("sent")
            return 0
        except RpcFailure as exc:
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
