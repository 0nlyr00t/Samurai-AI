"""Does anything answer at the target, before a run spends tokens on it.

A dead target used to be discovered by the agent, one wasted step at a time. The
preflight asks the cheap question first: any of the usual service ports, or at
least an ICMP echo.
"""

from __future__ import annotations

import ipaddress
import platform
import socket
import subprocess
from dataclasses import dataclass

#: the ports a first-contact preflight tries; all of them at most once
PROBE_PORTS = (22, 80, 443, 445, 3389, 8080)
CONNECT_TIMEOUT = 2.0
PING_TIMEOUT = 2


@dataclass(frozen=True)
class Probe:
    target: str
    address: str
    state: str  # open | silent | unresolved

    @property
    def answers(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True)
class Preflight:
    probes: tuple[Probe, ...]

    @property
    def answered(self) -> tuple[Probe, ...]:
        return tuple(probe for probe in self.probes if probe.answers)

    @property
    def silent(self) -> tuple[Probe, ...]:
        return tuple(probe for probe in self.probes if not probe.answers)

    @property
    def dead(self) -> bool:
        return bool(self.probes) and not self.answered


def is_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def probe_host(address: str, port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect((address, port))
        except TimeoutError:
            return "timeout"
        except OSError as exc:
            return "refused" if exc.errno in (61, 111) else "error"
    return "open"


def pings(address: str) -> bool:
    """ICMP echo, best effort: a filtered ping is not a dead host."""
    flag = "-W" if platform.system() == "Darwin" else "-w"
    try:
        done = subprocess.run(
            ["ping", "-c", "1", flag, str(PING_TIMEOUT), address],
            capture_output=True,
            timeout=PING_TIMEOUT + 3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def resolve(target: str) -> str | None:
    if is_address(target):
        return target
    try:
        return socket.gethostbyname(target)
    except OSError:
        return None


def probe(target: str) -> Probe:
    address = resolve(target)
    if address is None:
        return Probe(target=target, address="", state="unresolved")
    for port in PROBE_PORTS:
        if probe_host(address, port) == "open":
            return Probe(target=target, address=address, state="open")
    if pings(address):
        return Probe(target=target, address=address, state="open")
    return Probe(target=target, address=address, state="silent")


def preflight(targets: list[str]) -> Preflight:
    """Probe every target once; the caller decides what a fully silent run means."""
    return Preflight(probes=tuple(probe(target) for target in targets))


__all__ = [
    "CONNECT_TIMEOUT",
    "PROBE_PORTS",
    "Preflight",
    "Probe",
    "is_address",
    "pings",
    "preflight",
    "probe",
    "probe_host",
    "resolve",
]
