"""RDP session backend: one desktop master with per-command request drivers."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

from samurai_ai.execution.native_sandbox import build_bwrap_argv
from samurai_ai.execution.process import ManagedProcess, ProcessManager, ProcessResult
from samurai_ai.execution.remote import _RDP_EXEC_DRIVER
from samurai_ai.services.sessions import Session, SessionError

_RDP_HELPERS = _RDP_EXEC_DRIVER.split("\nuser, command = sys.argv[1:3]", 1)[0]

def rdp_http_fetch(url: str) -> tuple[int, str]:
    """Bounded DevTools/HTTP alternative to coordinate-driven web actions."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("GUI:fetch accepts only http(s) URLs")
    with urlopen(url, timeout=15) as response:  # noqa: S310 - explicit operator URL
        body = response.read(262144).decode("utf-8", errors="replace")
        return int(response.status), body


_RDP_MASTER_MAIN = r'''
import io, contextlib
user, endpoint, control_dir = sys.argv[1:4]
host, port = endpoint.rsplit(":", 1)
password = os.environ.get("SAMURAI_RDP_PASS", "")
local = control_dir
requests = os.path.join(local, "requests")
results = os.path.join(local, "results")
os.makedirs(requests, exist_ok=True)
os.makedirs(results, exist_ok=True)
display_process = None
session = None

def execute_terminal(driver, session, command, run_id):
    if os.environ.get("SAMURAI_RDP_SHELL") == "cmd":
        output = "\\\\tsclient\\samurai\\" + run_id + ".out"
        marker = "\\\\tsclient\\samurai\\" + run_id + ".code"
        line = "(" + command + ") > " + output + " 2>&1 & echo %ERRORLEVEL% > " + marker
    else:
        line = "export SAMURAI_RDP_PASS=" + shlex.quote(password) + "; sh -c " + shlex.quote(
            "(" + command + ") > ~/thinclient_drives/samurai/" + run_id + ".out 2>&1; "
            "printf '%s' \"$?\" > ~/thinclient_drives/samurai/" + run_id + ".code"
        )
    subprocess.run([driver, "type", "--delay", "12", line], check=True)
    subprocess.run([driver, "key", "Return"], check=True)
    if "sudo " in command:
        time.sleep(2)
        subprocess.run([driver, "type", "--delay", "12", password], check=False)
        subprocess.run([driver, "key", "Return"], check=False)
    marker = os.path.join(local, run_id + ".code")
    output = os.path.join(local, run_id + ".out")
    deadline = time.time() + 280
    while not os.path.exists(marker):
        if session.poll() is not None:
            raise RuntimeError("RDP session ended before command completion")
        if time.time() > deadline:
            raise TimeoutError("RDP command timed out")
        time.sleep(0.2)
    code = int(open(marker).read().strip() or "1")
    text = open(output, errors="replace").read() if os.path.exists(output) else ""
    return code, text

try:
    files = json.loads(os.environ.get("SAMURAI_RDP_FILES", "{}"))
    for name, content in files.items():
        if name != os.path.basename(name) or not name or len(content.encode()) > 65536:
            raise ValueError("RDP file names must be bare and each file at most 64 KiB")
        with open(os.path.join(local, name), "w", encoding="utf-8") as handle:
            handle.write(content)
    display_name, display_process = display()
    os.environ["DISPLAY"] = display_name
    binary = shutil.which("rdesktop")
    driver = shutil.which("xdotool")
    if not binary or not driver:
        raise RuntimeError("RDP exec requires rdesktop and xdotool on the host")
    shell = os.environ.get("SAMURAI_RDP_SHELL", "posix")
    session = subprocess.Popen(
        [binary, "-r", "disk:samurai=" + local, "-u", user, "-p", "-",
         "-s", "cmd.exe" if shell == "cmd" else "xterm", host + ":" + port],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    answer_prompts(session, password)
    time.sleep(2)
    # The remote terminal lives inside the single rdesktop window, not as a
    # separate local X11 window, so xdotool search cannot discover it.
    with open(os.path.join(local, "ready.json"), "w") as handle:
        handle.write("{}")
    while True:
        for name in sorted(os.listdir(requests)):
            path = os.path.join(requests, name)
            try:
                request = json.load(open(path))
                action = request["action"]
                run_id = request["id"]
                if action == "exec":
                    command = request["command"]
                    if command.startswith("GUI:fetch="):
                        from samurai_ai.services.rdp_sessions import rdp_http_fetch
                        status, body = rdp_http_fetch(command[10:])
                        result = {"exit_code": status, "stdout": body, "stderr": ""}
                    elif command.startswith("GUI:"):
                        stream = io.StringIO()
                        with contextlib.redirect_stdout(stream):
                            gui_actions(driver, command[4:], local)
                        result = {"exit_code": 0, "stdout": stream.getvalue(), "stderr": ""}
                    else:
                        code, text = execute_terminal(driver, session, command, run_id)
                        result = {"exit_code": code, "stdout": text, "stderr": ""}
                elif action == "write":
                    data = base64.b64decode(request["data"]).decode()
                    subprocess.run([driver, "type", "--delay", "12", data], check=False)
                    result = {"exit_code": 0, "stdout": "", "stderr": ""}
                elif action == "exit":
                    result = {"exit_code": 0, "stdout": "", "stderr": ""}
                else:
                    raise ValueError("unknown RDP action: " + action)
                with open(os.path.join(results, run_id + ".json"), "w") as handle:
                    json.dump(result, handle)
                if action == "exit":
                    raise SystemExit(0)
            except Exception as error:
                with open(os.path.join(results, request.get("id", name) + ".json"), "w") as handle:
                    json.dump({"exit_code": 3, "stdout": "", "stderr": str(error)}, handle)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        time.sleep(0.1)
except Exception as error:
    with open(os.path.join(local, "error.json"), "w") as handle:
        handle.write(str(error))
    raise
finally:
    if session is not None:
        session.terminate()
        try:
            session.wait(timeout=2)
        except subprocess.TimeoutExpired:
            session.kill()
    if display_process is not None:
        display_process.terminate()
'''

_RDP_MASTER_DRIVER = _RDP_HELPERS + _RDP_MASTER_MAIN

_RDP_COMMAND_DRIVER = r'''
import base64, json, os, select, sys, time
control_dir, request_id, command = sys.argv[1:4]
requests = os.path.join(control_dir, "requests")
results = os.path.join(control_dir, "results")
os.makedirs(requests, exist_ok=True)
os.makedirs(results, exist_ok=True)
request = {"id": request_id, "action": "exec", "command": command}
temporary = os.path.join(requests, request_id + ".tmp")
with open(temporary, "w") as handle:
    json.dump(request, handle)
os.replace(temporary, os.path.join(requests, request_id + ".json"))
result_path = os.path.join(results, request_id + ".json")
deadline = time.time() + 280
while not os.path.exists(result_path):
    if time.time() > deadline:
        raise SystemExit(124)
    ready, _, _ = select.select([0], [], [], 0.1)
    if ready:
        data = os.read(0, 65536)
        if data:
            write_id = request_id + "-write-" + str(time.time_ns())
            request = {"id": write_id, "action": "write", "data": base64.b64encode(data).decode()}
            temporary = os.path.join(requests, write_id + ".tmp")
            with open(temporary, "w") as handle:
                json.dump(request, handle)
            os.replace(temporary, os.path.join(requests, write_id + ".json"))
result = json.load(open(result_path))
sys.stdout.write(result.get("stdout", ""))
sys.stderr.write(result.get("stderr", ""))
raise SystemExit(int(result.get("exit_code", 1)))
'''


class RdpBackend:
    def __init__(self, manager: ProcessManager, autonomy_required: bool = False) -> None:
        self.manager = manager
        self.autonomy_required = autonomy_required

    async def start(self, session: Session) -> str:
        control_dir = self._control_dir(session)
        await asyncio.to_thread(shutil.rmtree, control_dir, True)
        await asyncio.to_thread(os.makedirs, control_dir, exist_ok=True)
        endpoint = session.metadata.get("endpoint", f"{session.host}:{session.port}")
        env = dict(session.env)
        if self.autonomy_required:
            # the host X socket is hidden by the private bwrap /tmp; create ours
            env.pop("DISPLAY", None)
        env["SAMURAI_RDP_PASS"] = env.get("SAMURAI_SSH_PASS", "")
        env["SAMURAI_RDP_FILES"] = session.metadata.get("files", "{}")
        env["SAMURAI_RDP_SHELL"] = session.metadata.get("shell", "posix")
        handle = await self.manager.start(
            self._confine(
                [
                    "python3",
                    "-c",
                    _RDP_MASTER_DRIVER,
                    session.user,
                    endpoint,
                    control_dir,
                ]
            ),
            env=env,
        )
        session.master_process_id = handle.id
        try:
            await self._wait_ready(control_dir)
        except Exception as exc:
            result = await self.manager.wait(handle.id, timeout_seconds=1)
            detail = (result.stdout + result.stderr).strip()
            raise SessionError(f"{exc}: {detail}") from exc
        return handle.id

    async def exec(self, session: Session, command: str, pty: bool) -> ManagedProcess:
        request_id = uuid4().hex[:16]
        return await self.manager.start(
            self._confine(
                [
                    "python3",
                    "-c",
                    _RDP_COMMAND_DRIVER,
                    self._control_dir(session),
                    request_id,
                    command,
                ]
            ),
            env=session.env,
            keep_stdin_open=True,
        )

    async def control(self, session: Session, operation: str) -> ProcessResult:
        control_dir = Path(self._control_dir(session))
        if operation == "check":
            if session.master_process_id and self.manager.status(
                session.master_process_id
            ) != "running":
                return ProcessResult(exit_code=1, stdout="", stderr="")
            ready = await asyncio.to_thread((control_dir / "ready.json").exists)
            return ProcessResult(exit_code=0 if ready else 1, stdout="", stderr="")
        if operation == "exit":
            request_id = f"exit-{uuid4().hex[:12]}"
            requests = control_dir / "requests"
            results = control_dir / "results"
            await asyncio.to_thread(requests.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(results.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                (requests / f"{request_id}.json").write_text,
                json.dumps({"id": request_id, "action": "exit"}),
            )
            return ProcessResult(exit_code=0, stdout="", stderr="")
        raise SessionError(f"unsupported RDP control operation {operation}")

    async def cleanup(self, session: Session) -> None:
        await asyncio.to_thread(shutil.rmtree, self._control_dir(session), True)

    async def _wait_ready(self, control_dir: str) -> None:
        for _ in range(300):
            if await asyncio.to_thread(Path(control_dir, "ready.json").exists):
                return
            error = Path(control_dir, "error.json")
            if await asyncio.to_thread(error.exists):
                detail = await asyncio.to_thread(error.read_text)
                raise SessionError(f"RDP master failed: {detail}")
            await asyncio.sleep(0.1)
        raise SessionError("RDP master did not become ready")

    def _confine(self, argv: list[str]) -> list[str]:
        return (
            build_bwrap_argv(argv, ("/tmp",), strict=False)
            if self.autonomy_required
            else argv
        )

    def _control_dir(self, session: Session) -> str:
        return session.socket_path + ".rdp"
