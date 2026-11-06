"""`samurai run`: target, plan and execution in one command.

The short path is the point: an operator who just wants the harness to work on a
box should not have to assemble an operation, a plan file and an approval id.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from samurai_ai.config import serve_socket_path
from samurai_ai.domain.scope import ScopeContract, ScopeValidationError


def add_run_command(subcommands: Any, add_parser: Any) -> None:
    run = add_parser("run", help="Create the operation, the plan and start working")
    run.add_argument("name")
    from samurai_ai.domain.operations import OperationMode

    run.add_argument(
        "--mode", required=True, choices=[mode.value for mode in OperationMode]
    )
    run.add_argument("--include", action="append", dest="includes", required=True)
    run.add_argument("--exclude", action="append", dest="excludes", default=[])
    run.add_argument("--task", action="append", dest="tasks", default=[], metavar="TITLE")
    run.add_argument("--objective", default="")
    run.add_argument(
        "--relay",
        default="",
        metavar="PREFIX",
        help="run network tools through this prefix, e.g. prlctl exec <vm-uuid>",
    )
    run.add_argument(
        "--fallback",
        action="append",
        default=[],
        dest="fallbacks",
        metavar="MODEL_ID",
        help="try this model when the first one is unavailable",
    )
    run.add_argument(
        "--no-relay-host",
        dest="relay_host",
        action="store_false",
        help="do not add the relay host's own address to the scope",
    )
    run.add_argument(
        "--no-preflight",
        action="store_true",
        help="start even when no target answers",
    )
    run.add_argument(
        "--offline",
        action="store_true",
        help="the harness has no internet: local models only, no outside tools",
    )
    run.add_argument("--model", dest="model_id", required=True)
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--socket", default=argparse.SUPPRESS)
    run.add_argument(
        "--detach",
        action="store_true",
        help="start through the daemon and leave it running in the background",
    )
    run.set_defaults(handler=_run)


def _prepare(args: argparse.Namespace, operations: Any, kernel: Any) -> tuple[str, str]:
    scope = ScopeContract(includes=tuple(args.includes), excludes=tuple(args.excludes))
    operation = operations.create(
        args.name,
        _mode(args.mode),
        scope,
        objective=args.objective,
        offline=args.offline,
        relay=args.relay,
    )
    tasks = [title.strip() for title in args.tasks if title.strip()]
    if not tasks:
        raise ValueError("give at least one --task to work through")
    from samurai_ai.domain.kernel import PlanTask

    steps = [
        PlanTask(id=f"step{i}", title=title, depends_on=(f"step{i - 1}",) if i > 1 else ())
        for i, title in enumerate(tasks, start=1)
    ]
    plan = kernel.create_plan(operation.id, args.name, steps, [])
    kernel.approve_plan(operation.id)
    return operation.id, plan.id


def _require_model(models: Any, model_id: str) -> None:
    from samurai_ai.domain.models import ModelNotFoundError

    known = {model.id for model in models.list_models()}
    if model_id not in known:
        raise ModelNotFoundError(f"unknown model {model_id}")


def _mode(value: str) -> Any:
    from samurai_ai.domain.operations import OperationMode

    return OperationMode(value)


def _preflight(args: argparse.Namespace) -> bool:
    """Ask the cheap question first: does anything answer at all?"""
    from samurai_ai.services.reachability import preflight

    report = preflight(list(args.includes))
    for probe in report.silent:
        where = probe.address or probe.target
        print(f"warn: {where} does not answer on {_port_list()}", file=sys.stderr)
    if report.dead and not args.no_preflight:
        print(
            "error: no target answers — check the VPN/route, or start with "
            "--no-preflight",
            file=sys.stderr,
        )
        return False
    return True


def _relay_report(relay: str, operation_id: str, operations: Any) -> None:
    """Say up front what the far side has, and put it where the agent reads it."""
    from samurai_ai.agents.blackboard import make_note_event
    from samurai_ai.domain.execution import SecretRedactor
    from samurai_ai.execution.relay import PROBE_TOOLS, probe_tools

    present = probe_tools(relay)
    missing = [name for name in PROBE_TOOLS if not present[name]]
    if missing:
        print(f"warn: relay has no {', '.join(missing)}", file=sys.stderr)
    text = (
        f"relay `{relay}`; available there: "
        + (", ".join(name for name in PROBE_TOOLS if present[name]) or "nothing")
        + ("; missing: " + ", ".join(missing) if missing else "")
    )
    operations.repository.append_event(
        make_note_event(operation_id, "operator", "shared.relay.tools", text, SecretRedactor())
    )


def _allow_relay_host(relay: str, operation_id: str, operations: Any) -> None:
    """The VM is part of the picture; in scope it stops being a refused literal."""
    from samurai_ai.domain.scope import ScopeContract, ScopeValidationError
    from samurai_ai.execution.relay import relay_host

    address = relay_host(relay)
    if not address:
        return
    operation = operations.get(operation_id)
    if address in operation.scope.includes:
        return
    try:
        scope = ScopeContract(
            includes=(*operation.scope.includes, address),
            excludes=operation.scope.excludes,
            hosts=operation.scope.hosts,
        )
    except ScopeValidationError:
        return
    operations.replace_scope(operation_id, scope)
    print(f"note: relay host {address} added to the scope", file=sys.stderr)


def _port_list() -> str:
    from samurai_ai.services.reachability import PROBE_PORTS

    return "/".join(str(port) for port in PROBE_PORTS)


def _run(args: argparse.Namespace) -> int:
    from samurai_ai.cli import _make_services

    if not _preflight(args):
        return 2
    services = _make_services(getattr(args, "db", None))
    operations, kernel, swarms, models = services[0], services[1], services[14], services[7]
    from samurai_ai.domain.models import ModelNotFoundError

    try:
        # a bad model id should cost a second, not a half-built operation
        models.list_models()
        _require_model(models, args.model_id)
        for fallback in args.fallbacks:
            _require_model(models, fallback)
        operation_id, plan_id = _prepare(args, operations, kernel)
    except ModelNotFoundError as exc:
        print(
            f"error: {exc}\nregister it first: samurai model register … then model add …",
            file=sys.stderr,
        )
        return 2
    except (ScopeValidationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.relay:
        _relay_report(args.relay, operation_id, operations)
        if args.relay_host:
            _allow_relay_host(args.relay, operation_id, operations)
    socket_path = Path(getattr(args, "socket", None) or serve_socket_path())
    if socket_path.exists():
        return _through_daemon(args, socket_path, operation_id, plan_id)
    print(f"operation {operation_id}, plan {plan_id}")
    return _inline(args, swarms, operation_id)


def _through_daemon(
    args: argparse.Namespace, socket_path: Path, operation_id: str, plan_id: str
) -> int:
    from samurai_ai.services.rpc.client import Client, RpcFailure, format_event

    async def run() -> int:
        try:
            client = await Client.connect(socket_path, client="run")
        except RpcFailure as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            started = await client.call(
                "run.start",
                {"operation_id": operation_id, "model_id": args.model_id,
                 "workers": args.workers, "fallbacks": list(args.fallbacks)},
            )
            print(f"operation {operation_id}, plan {plan_id}, run {started['run_id']}")
            if args.detach:
                print(f"follow with: samurai attach {operation_id}")
                return 0
            await client.subscribe(operation_id, 0)
            async for frame in client.stream():
                print(format_event(frame), flush=True)
                if frame.get("event") == "agent.run.finish":
                    break
            return 0 if (await client.call("run.list", {"operation_id": operation_id}))[0][
                "state"
            ] == "done" else 1
        except RpcFailure as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            await client.close()

    return asyncio.run(run())


def _inline(args: argparse.Namespace, swarms: Any, operation_id: str) -> int:
    route = [args.model_id, *args.fallbacks]
    approval = swarms.prepare_model_approval(operation_id, route)
    return _swarm_until_done(args, swarms, operation_id, approval.id, route)


def _swarm_until_done(
    args: argparse.Namespace,
    swarms: Any,
    operation_id: str,
    approval_id: str,
    route: list[str],
) -> int:
    """A plan is a chain: one swarm only reaches its ready tasks, so loop."""
    from samurai_ai.domain.kernel import TaskStatus

    done = 0
    failed = 0
    while True:
        ready = swarms.ready_tasks(operation_id)
        if not ready:
            break
        swarm = swarms.start(
            operation_id,
            [task.id for task in ready],
            route,
            workers=args.workers,
            approval_id=approval_id,
            approval_resolver=lambda _id: True,
        )
        report = swarms.report(swarm.id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # the snapshot taken before the swarm is stale: read the plan again
        plan = swarms.kernel.get_current_plan(operation_id)
        by_id = {task.id: task for task in plan.tasks} if plan else {}
        for task in ready:
            if by_id.get(task.id) is not None and by_id[task.id].status is TaskStatus.DONE:
                done += 1
            else:
                failed += 1
        if swarm.status.value == "failed":
            break
    print(f"tasks done={done} failed={failed}")
    return 0 if failed == 0 and done else 1


__all__ = ["add_run_command"]
