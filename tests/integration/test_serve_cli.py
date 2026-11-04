"""`samurai serve` end to end: real processes, real socket, real second terminal."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from samurai_ai.cli import main


@pytest.fixture
def stand(monkeypatch):
    directory = Path(tempfile.mkdtemp(dir="/tmp", prefix="samurai-serve-"))
    socket_path = directory / "s.sock"
    env = {
        **os.environ,
        "SAMURAI_HOME": str(directory),
        "PYTHONPATH": str(Path.cwd() / "src"),
    }
    database = str(directory / "s.db")
    process = subprocess.Popen(
        [sys.executable, "-m", "samurai_ai", "--db", database, "serve",
         "--socket", str(socket_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(200):
        if socket_path.exists():
            break
        if process.poll() is not None:
            raise AssertionError(process.stdout.read() if process.stdout else "daemon died")
        time.sleep(0.05)
    try:
        yield database, socket_path, directory
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_second_terminal_writes_and_reads(stand, capsys) -> None:
    database, socket_path, directory = stand
    assert main(["--db", database, "--json", "operation", "create", "Live",
                 "--mode", "red-team", "--include", "127.0.0.1"]) == 0
    operation_id = json.loads(capsys.readouterr().out)["id"]
    assert main(["--db", database, "--socket", str(socket_path), "send",
                 operation_id, "--message", "смотри 22 порт"]) == 0
    assert capsys.readouterr().out.strip() == "sent"
    assert main(["--db", database, "--socket", str(socket_path), "attach",
                 operation_id, "--once"]) == 0
    output = capsys.readouterr().out
    assert "operator.message" in output and "смотри 22 порт" in output
    # a read-only client cannot write
    assert main(["--db", database, "--socket", str(socket_path), "call",
                 "send.message", "--read-only",
                 "--params", json.dumps({"operation_id": operation_id, "message": "x"})]) == 1
    assert "read-only" in capsys.readouterr().err
    # the daemon wrote its pid where the next start looks for it
    assert (directory / "run" / "serve.pid").exists()


def test_attach_without_a_daemon_fails_cleanly(stand, capsys) -> None:
    database, socket_path, directory = stand
    assert main(["--db", database, "--socket", str(directory / "nope.sock"), "attach", "op_1"]) == 1
    assert "no live daemon" in capsys.readouterr().err
