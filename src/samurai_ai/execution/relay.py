"""Run a network tool through a relay prefix instead of a container.

A VPN often lives one hop away — in a Parallels/UTM guest, behind an SSH jump
host. The host has no route to the target, so every container the sandbox spawns
is blind, and the only working path used to be a raw shell. The relay prefix
(``prlctl exec '<uuid>'``, ``ssh -J jump``) puts the tool on the far side of that
hop while the harness keeps the scope guard, the artifact trail and the parser.

The container is skipped entirely: ``bash -lc "<relay> <tool> <args>"`` runs on
the host. Fixed argv survives the trip quoted, so a model-supplied value cannot
break out of the command.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shlex
from dataclasses import dataclass
from html import unescape as _unescape
from typing import Any

from samurai_ai.domain.execution import ToolExecutionError, ToolValidationError
from samurai_ai.domain.sandbox import SandboxProfile
from samurai_ai.execution.egress import DEFAULT_ALLOWED_PORTS
from samurai_ai.execution.sandbox import DockerSandboxRunner

#: what a relay prefix may look like: no NUL, no leading dash (it is not argv)
MAX_RELAY_LENGTH = 400


def validate_relay(relay: str) -> str:
    """Reject a prefix that would not survive `bash -lc`."""
    value = relay.strip()
    if not value:
        raise ToolValidationError("relay prefix is empty")
    if len(value) > MAX_RELAY_LENGTH:
        raise ToolValidationError(f"relay prefix is longer than {MAX_RELAY_LENGTH}")
    if "\x00" in value:
        raise ToolValidationError("relay prefix cannot contain NUL bytes")
    return value


def relay_command(relay: str, executable: str, args: list[str]) -> str:
    """The single shell line a relayed call becomes.

    Join-style relays (prlctl exec, ssh) pass argv through a far-side shell
    that re-parses the joined line: quotes spent on the host side never reach
    the guest, so `bash -c 'echo hello'` used to arrive as `bash -c echo hello`
    and run an empty echo. Every argument is therefore quoted twice — once for
    the host shell, once for the far side.
    """
    doubled = [shlex.quote(shlex.quote(arg)) for arg in (executable, *args)]
    return f"{relay} {' '.join(doubled)}"


def normalize_relayed(relay: str, command: str) -> str:
    """Rewrite a model-written command that already starts with the relay.

    The model types `prlctl exec '<uuid>' -- bash -c '...'` by hand and loses
    to the relay's re-splitting (a stray `--`, a vanished quote). When the
    command already begins with the relay prefix, rebuild the tail through
    `relay_command` so it arrives quoted for the far side. Anything else is
    left alone.
    """
    try:
        relay_tokens = shlex.split(relay)
        tokens = shlex.split(command)
    except ValueError:
        return command
    if not relay_tokens or tokens[: len(relay_tokens)] != relay_tokens:
        return command
    tail = tokens[len(relay_tokens):]
    if tail and tail[0] == "--":
        tail = tail[1:]
    if not tail:
        return command
    return relay_command(relay, tail[0], tail[1:])


class RelaySandboxRunner(DockerSandboxRunner):
    """Docker runner that hands the command to a relay prefix instead."""

    def __init__(
        self,
        manager: Any,
        signature_path: Any,
        master_key_loader: Any,
        relay: str,
        egress_allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
    ) -> None:
        super().__init__(
            manager,
            signature_path,
            master_key_loader,
            egress_allowed_ports=egress_allowed_ports,
        )
        self.relay = validate_relay(relay)

    def build_argv(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
        proxy_url: str | None = None,
        env: dict[str, str] | None = None,
        secret_env: dict[str, str] | None = None,
        hosts: dict[str, str] | None = None,
        network: str | None = None,
    ) -> list[str]:
        del run_id, proxy_url, hosts, network
        # the same allowlist and NUL checks as the container path
        self.validate_invocation(profile, executable, args)
        if secret_env:
            self.validate_secret_env(profile, secret_env)
        return ["bash", "-lc", relay_command(self.relay, executable, args)]

    async def preflight(self, profile: SandboxProfile) -> str | None:
        """No image to look for: the far side owns its own tooling."""
        return self.verify_allowlist_signature()


@dataclass(frozen=True)
class RelayResponse:
    """What a relayed fetch answered."""

    status: int
    headers: str
    body: bytes


def relayed_fetch(
    relay: str,
    url: str,
    *,
    method: str,
    timeout: float,
    max_bytes: int,
    manager: Any,
) -> RelayResponse:
    """Fetch a URL with curl on the far side of the relay.

    The host has no route, so its own http.client cannot connect; curl runs
    where the route exists and only travel-safe limits are applied here.
    """
    argv = [
        "bash",
        "-lc",
        relay_command(
            relay,
            "curl",
            [
                "-sS",
                "-i",
                "--max-time",
                str(max(1, int(timeout))),
                # no --max-filesize: a bigger page must be truncated by us,
                # not failed by curl (exit 63)
                "-X",
                method,
                url,
            ],
        ),
    ]
    result = asyncio.run(manager.run(argv, timeout_seconds=timeout + 5))
    if result.exit_code not in (0, None) or result.timed_out:
        detail = (result.stderr or result.stdout).strip()[:500]
        raise ToolExecutionError(f"relayed fetch failed for {url}: {detail}")
    raw = result.stdout.encode("utf-8", errors="surrogateescape")
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").splitlines()
    status = 0
    if lines and lines[0].startswith("HTTP/"):
        parts = lines[0].split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers = "\r\n".join(lines[1:])
    return RelayResponse(status=status, headers=headers, body=body[:max_bytes])


#: tools the harness offers but the far side may not have
PROBE_TOOLS = ("nmap", "curl", "ffuf", "nuclei", "gobuster", "hydra", "python3", "httpx")


def probe_tools(
    relay: str, names: tuple[str, ...] = PROBE_TOOLS, *, timeout: float = 30.0
) -> dict[str, bool]:
    """Which tools exist on the far side of the relay.

    Offering a tool the relay host lacks costs a whole step per attempt: this
    asks once, in one command, and lets the prompt name what actually works.
    """
    from samurai_ai.execution.process import ProcessManager

    fragments = []
    for name in names:
        if name == "httpx":
            # the python httpx CLI answers `command -v` too, but has no
            # scanner flags — offer the tool only for the real scanner
            fragments.append(
                "command -v httpx >/dev/null"
                " && httpx -h 2>/dev/null | grep -q -- -silent && echo httpx"
            )
        else:
            fragments.append(f"command -v {name} >/dev/null && echo {name}")
    script = "; ".join(fragments)
    argv = ["bash", "-lc", relay_command(relay, "bash", ["-c", script])]
    manager = ProcessManager(allowed_executables=frozenset({"bash"}))
    try:
        result = asyncio.run(manager.run(argv, timeout_seconds=timeout))
    except Exception:  # noqa: BLE001 - a failed probe is an empty answer
        return {name: False for name in names}
    found = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return {name: name in found for name in names}


_TAG = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_LINK = re.compile(r"""href=["']([^"'#]+)["']""", re.I)
_BLANKS = re.compile(r"\n{3,}")


def html_to_text(html: str) -> tuple[str, list[str], str]:
    """Title, links and readable text from a page, without a browser.

    A relayed read loses the browser: enough stays for the model to work with
    (what the page says, where it points), and the raw body is still an artifact.
    """
    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if match:
        title = " ".join(match.group(1).split())[:200]
    links: list[str] = [str(link) for link in _LINK.findall(html)][:50]
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _TAG.sub("\n", text)
    text = _unescape(text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    return title, links, _BLANKS.sub("\n\n", text).strip()


_PROBE_SCRIPT = (
    "import socket,sys\n"
    "addr=sys.argv[1]\n"
    "hit=\"\"\n"
    "for p in sys.argv[2:]:\n"
    " s=socket.socket();s.settimeout(2)\n"
    " try:\n"
    "  s.connect((addr,int(p)));hit=p;break\n"
    " except Exception:pass\n"
    " finally:s.close()\n"
    "print(\"open \"+hit if hit else \"silent\")"
)


def relayed_probe(
    relay: str, address: str, ports: tuple[int, ...], *, timeout: float = 30.0
) -> str:
    """One connect probe per port, run on the far side of the relay.

    The host has no route to the target — that is the point of the relay — so
    a local socket answers a question about the wrong network. The probe is a
    python3 one-shot: every far side that can run tools has it.
    """
    from samurai_ai.execution.process import ProcessManager

    argv = [
        "bash",
        "-lc",
        relay_command(
            relay, "python3", ["-c", _PROBE_SCRIPT, address, *(str(p) for p in ports)]
        ),
    ]
    manager = ProcessManager(allowed_executables=frozenset({"bash"}))
    try:
        result = asyncio.run(manager.run(argv, timeout_seconds=timeout))
    except Exception:  # noqa: BLE001 - a failed probe is a silent answer
        return "error"
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("open"):
            return "open"
        if line == "silent":
            return "silent"
    return "error"


def relay_host(relay: str, *, timeout: float = 20.0) -> str:
    """The relay host's own first IPv4 address, or an empty string.

    The agent keeps reaching for it ("the machine I am going through"), so the
    address belongs in scope; we ask the far side instead of parsing the prefix.
    """
    from samurai_ai.execution.process import ProcessManager

    # no inner `bash -c`: prlctl re-splits a quoted string, so the flag must be
    # an argument of the program itself
    argv = ["bash", "-lc", relay_command(relay, "hostname", ["-I"])]
    manager = ProcessManager(allowed_executables=frozenset({"bash"}))
    try:
        result = asyncio.run(manager.run(argv, timeout_seconds=timeout))
    except Exception:  # noqa: BLE001 - an unanswered probe is just "no address"
        return ""
    for token in result.stdout.split():
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue
        if not token.startswith(("127.", "::")):
            return token
    return ""


def build_runner(relay: str) -> RelaySandboxRunner:
    """A relay runner wired to a host shell, ready to hand to an adapter."""
    from samurai_ai.config import sandbox_allowlist_path, vault_key_path
    from samurai_ai.execution.process import ProcessManager
    from samurai_ai.services.vault import load_master_key

    return RelaySandboxRunner(
        # a relayed tool's output arrives at process end (prlctl buffers the
        # far side), so silence is normal — the stall watchdog is off here and
        # the per-run timeout alone bounds the work
        ProcessManager(allowed_executables=frozenset({"bash"}), stall_seconds=0),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
        relay,
    )


__all__ = [
    "MAX_RELAY_LENGTH",
    "RelaySandboxRunner",
    "build_runner",
    "PROBE_TOOLS",
    "probe_tools",
    "html_to_text",
    "relay_host",
    "relay_command",
    "relayed_fetch",
    "RelayResponse",
    "validate_relay",
]
