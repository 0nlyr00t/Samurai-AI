"""Membership and notes for an operation.

Membership is checked after the global gate, never instead of it. An operation
with no members is open to any signed-in operator — that is what keeps the
existing single-user installs working after the upgrade.
"""

from __future__ import annotations

from samurai_ai.domain.access import Principal, Role
from samurai_ai.domain.operations import Operation
from samurai_ai.domain.teamwork import (
    BODY_MAX,
    NOTE_TAIL,
    Member,
    NotAMemberError,
    NoteTarget,
    OperationLevel,
    OperationNote,
    TeamworkError,
    effective_level,
)
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.access import AccessRepository
from samurai_ai.storage.teamwork import TeamworkRepository


class TeamworkService:
    def __init__(
        self,
        operations: OperationService,
        access: AccessRepository,
        teamwork: TeamworkRepository,
    ) -> None:
        self.operations = operations
        self.access = access
        self.teamwork = teamwork

    # --- adding people ----------------------------------------------------
    def claim_owner(self, operation_id: str, principal: Principal) -> None:
        """The creator owns what they created; a second caller changes nothing."""
        if self.teamwork.list_members(operation_id):
            return
        self.teamwork.set_owner(operation_id, principal.subject, principal.label)

    def add_member(
        self,
        operation_id: str,
        subject: str,
        *,
        level: OperationLevel = OperationLevel.MEMBER,
        actor: Principal,
    ) -> Member:
        self.require(operation_id, actor, OperationLevel.OWNER)
        subject = subject.strip()
        if not subject:
            raise TeamworkError("a member needs a subject")
        if level is OperationLevel.OWNER:
            raise TeamworkError("use the transfer command to hand over ownership")
        label = self._known_label(subject)
        if label is None:
            raise TeamworkError(
                f"nobody with subject {subject!r} has signed in on this install"
            )
        if subject == actor.subject:
            raise TeamworkError("the owner is already on the operation")
        self.teamwork.add_member(
            Member(
                operation_id=operation_id,
                subject=subject,
                label=label,
                level=level,
                added_by=actor.subject,
            )
        )
        member = self.teamwork.get_member(operation_id, subject)
        assert member is not None
        return member

    def remove_member(self, operation_id: str, subject: str, *, actor: Principal) -> bool:
        self.require(operation_id, actor, OperationLevel.OWNER)
        if self.teamwork.get_member(operation_id, subject) is None:
            raise TeamworkError(f"{subject} is not on this operation")
        if self._owner_subject(operation_id) == subject:
            raise TeamworkError("the owner cannot be removed; transfer the operation first")
        return self.teamwork.remove_member(operation_id, subject)

    def transfer(self, operation_id: str, subject: str, *, actor: Principal) -> None:
        self.require(operation_id, actor, OperationLevel.OWNER)
        label = self._known_label(subject.strip())
        if label is None:
            raise TeamworkError(f"nobody with subject {subject!r} has signed in on this install")
        self.teamwork.set_owner(operation_id, subject.strip(), label)

    # --- reading ----------------------------------------------------------
    def members(self, operation_id: str) -> list[Member]:
        return self.teamwork.list_members(operation_id)

    def owner(self, operation_id: str) -> str | None:
        for member in self.teamwork.list_members(operation_id):
            if member.level is OperationLevel.OWNER:
                return member.label or member.subject
        return None

    def level(self, operation_id: str, principal: Principal) -> OperationLevel | None:
        member = self.teamwork.get_member(operation_id, principal.subject)
        return effective_level(principal, member)

    def require(
        self,
        operation_id: str,
        principal: Principal,
        needed: OperationLevel = OperationLevel.VIEWER,
    ) -> OperationLevel:
        held = self.level(operation_id, principal)
        if held is None:
            owner = self.owner(operation_id)
            raise NotAMemberError(
                f"{principal.label} is not on this operation"
                + (f"; {owner} owns it" if owner else "")
            )
        if held.rank < needed.rank:
            raise NotAMemberError(
                f"this needs the {needed.value} level on the operation; "
                f"{principal.label} is {held.value}"
            )
        return held

    def assert_visible(self, operation_id: str, principal: Principal) -> None:
        self.require(operation_id, principal, OperationLevel.VIEWER)

    def visible(self, operations: list[Operation], principal: Principal) -> list[Operation]:
        """Filter a listing: members and admins see theirs, nobody sees the rest."""
        if principal.role is Role.ADMIN:
            return operations
        return [
            operation
            for operation in operations
            if self.teamwork.get_member(operation.id, principal.subject) is not None
        ]

    # --- notes ------------------------------------------------------------
    def note(
        self,
        operation_id: str,
        target: NoteTarget,
        target_id: str,
        body: str,
        *,
        actor: Principal,
    ) -> OperationNote:
        self.require(operation_id, actor, OperationLevel.MEMBER)
        body = " ".join(body.split())[:BODY_MAX]
        if not body:
            raise TeamworkError("a note needs text")
        note = OperationNote(
            operation_id=operation_id,
            target_kind=target,
            target_id=target_id,
            body=body,
            author=actor.label,
        )
        self.teamwork.add_note(note)
        return note

    def notes(self, operation_id: str, limit: int = NOTE_TAIL) -> list[OperationNote]:
        return self.teamwork.list_notes(operation_id, limit)

    # --- helpers ----------------------------------------------------------
    def _is_member(self, operation_id: str, principal: Principal) -> bool:
        return self.teamwork.get_member(operation_id, principal.subject) is not None

    def _owner_subject(self, operation_id: str) -> str | None:
        for member in self.teamwork.list_members(operation_id):
            if member.level is OperationLevel.OWNER:
                return member.subject
        return None

    def _known_label(self, subject: str) -> str | None:
        """Only subjects that have signed in at least once can be added."""
        for session in self.access.list_sessions():
            if session.principal.subject == subject:
                return session.principal.label
        return None
