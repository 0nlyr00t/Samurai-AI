"""Who is on the other end, and what it may do.

A socket peer is only as trusted as the file permissions that let it connect, so
identity comes from the socket (same uid) — but authority still travels per call:
read-only peers may only reach read methods, and every method runs through the
same access table the CLI uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from samurai_ai.domain.access import Principal, Role


class PeerKind(StrEnum):
    IN_PROCESS = "in-process"
    SOCKET = "socket"


@dataclass
class Peer:
    principal: Principal
    kind: PeerKind
    read_only: bool = False
    client: str = "unknown"
    id: str = field(default_factory=lambda: f"peer_{uuid4().hex[:8]}")

    @property
    def label(self) -> str:
        return f"{self.client}#{self.id[-4:]}"

    def as_frame(self) -> dict[str, object]:
        return {
            "peer": self.id,
            "client": self.client,
            "read_only": self.read_only,
            "principal": self.principal.subject,
            "role": self.principal.role.value,
        }


def operator_peer(*, read_only: bool = False, client: str = "local") -> Peer:
    """The owner of this machine: same uid that could edit the state anyway."""
    return Peer(
        principal=Principal(subject="local", display="local operator", role=Role.ADMIN),
        kind=PeerKind.SOCKET,
        read_only=read_only,
        client=client,
    )


def in_process_peer(principal: Principal, client: str = "in-process") -> Peer:
    return Peer(principal=principal, kind=PeerKind.IN_PROCESS, client=client)


__all__ = ["Peer", "PeerKind", "in_process_peer", "operator_peer"]
