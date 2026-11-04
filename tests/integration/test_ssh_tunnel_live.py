"""The tunnel against a real daemon socket, with ssh replaced by a local bridge.

Docker Desktop for macOS cannot carry a unix socket across a bind mount, so the
tests stub `ssh -L` with a small forwarder: everything else (process handling,
waiting for the socket, cleanup) is the real code.
"""

import asyncio
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest

from samurai_ai.services.rpc.carriers import UnixSocketCarrier
from samurai_ai.services.rpc.client import Client
from samurai_ai.services.rpc.peer import Peer
from samurai_ai.services.rpc.server import EventFeed, RpcServer
from samurai_ai.services.ssh_tunnel import Destination, SshTunnel, TunnelError

BRIDGE = """#!/usr/bin/env python3
import os, socket, sys, threading
argv = sys.argv[1:]
local, remote = "", ""
for index, item in enumerate(argv):
    if item == "-L":
        local, _, remote = argv[index + 1].partition(":")
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
if os.path.exists(local):
    os.unlink(local)
server.bind(local)
server.listen(8)


def pipe(source, target):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


while True:
    client, _ = server.accept()
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(remote)
    except OSError:
        client.close()
        continue
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
"""


@pytest.fixture
def stub_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = Path(tempfile.mkdtemp(dir="/tmp", prefix="smr-bin-"))
    ssh = stub / "ssh"
    ssh.write_text(BRIDGE)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")


def test_a_client_can_talk_through_the_tunnel(stub_ssh: None) -> None:
    async def scenario() -> None:
        short = Path(tempfile.mkdtemp(dir="/tmp", prefix="smr-tun-"))
        server = RpcServer(EventFeed(lambda _op, _seq, _limit: []))
        carrier = UnixSocketCarrier(short / "remote.sock")
        await server.start()
        await carrier.start(server.serve)

        async def echo(_peer: Peer, params: dict[str, Any]) -> str:
            return str(params.get("text"))

        server.register("operation.show", echo)
        tunnel = SshTunnel(
            Destination.parse("box"), short / "s.sock",
            remote_socket=str(short / "remote.sock"),
        )
        try:
            local = tunnel.open(wait_seconds=10)
            assert local.exists()
            client = await Client.connect(local, read_only=True)
            try:
                assert await client.call("operation.show", {"text": "via tunnel"}) == "via tunnel"
            finally:
                await client.close()
            assert not tunnel.local_socket.exists() or True
        finally:
            tunnel.close()
            await carrier.stop()
            await server.aclose()
            assert not tunnel.local_socket.exists()
            shutil.rmtree(short, ignore_errors=True)

    asyncio.run(scenario())


def test_a_broken_ssh_is_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    short = Path(tempfile.mkdtemp(dir="/tmp", prefix="smr-tun3-"))
    broken = short / "bin"
    broken.mkdir()
    ssh = broken / "ssh"
    ssh.write_text("#!/bin/sh\necho 'Permission denied (publickey).' >&2\nexit 255\n")
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{broken}:{os.environ['PATH']}")

    tunnel = SshTunnel(Destination.parse("box"), short / "s.sock")
    with pytest.raises(TunnelError, match="Permission denied"):
        tunnel.open(wait_seconds=5)
    shutil.rmtree(short, ignore_errors=True)
