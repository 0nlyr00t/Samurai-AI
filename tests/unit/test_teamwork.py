"""Membership beats openness, but only ever upward."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.access import Principal, Role
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.teamwork import (
    NotAMemberError,
    NoteTarget,
    OperationLevel,
    TeamworkError,
)
from samurai_ai.services.operations import OperationService
from samurai_ai.services.teamwork import TeamworkService
from samurai_ai.storage.access import AccessRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.teamwork import TeamworkRepository

OWNER = Principal(subject="s-owner", email="owner@x", role=Role.OPERATOR)
GUEST = Principal(subject="s-guest", email="guest@x", role=Role.OPERATOR)
ADMIN = Principal(subject="s-admin", email="admin@x", role=Role.ADMIN)
READER = Principal(subject="s-reader", email="reader@x", role=Role.VIEWER)


@pytest.fixture
def stack(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "team.db")
    operations = OperationService(OperationRepository(database))
    access = AccessRepository(database)
    teamwork = TeamworkService(operations, access, TeamworkRepository(database))
    operation = operations.create("lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("x",)))
    teamwork.claim_owner(operation.id, OWNER)
    # the people in play must have signed in once to have a name
    for principal in (OWNER, GUEST, ADMIN, READER):
        access.add_session(_session(principal))
    return teamwork, operations, operation.id


def _session(principal: Principal):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from samurai_ai.domain.access import Session

    return Session(
        principal=principal, access_expires_at=datetime.now(UTC) + timedelta(hours=1)
    )


def test_the_creator_owns_the_operation(stack) -> None:
    teamwork, _operations, operation_id = stack

    assert teamwork.owner(operation_id) == "owner@x"
    assert teamwork.level(operation_id, OWNER) is OperationLevel.OWNER


def test_a_stranger_cannot_open_it(stack) -> None:
    teamwork, _operations, operation_id = stack

    with pytest.raises(NotAMemberError, match="is not on this operation"):
        teamwork.assert_visible(operation_id, GUEST)


def test_an_admin_needs_no_membership(stack) -> None:
    teamwork, _operations, operation_id = stack

    assert teamwork.level(operation_id, ADMIN) is OperationLevel.OWNER


def test_an_added_member_may_work_but_not_hand_over(stack) -> None:
    teamwork, _operations, operation_id = stack
    teamwork.add_member(operation_id, "s-guest", actor=OWNER)

    assert teamwork.level(operation_id, GUEST) is OperationLevel.MEMBER
    teamwork.require(operation_id, GUEST, OperationLevel.MEMBER)
    with pytest.raises(NotAMemberError, match="owner level"):
        teamwork.require(operation_id, GUEST, OperationLevel.OWNER)


def test_a_viewer_guest_may_only_read(stack) -> None:
    teamwork, _operations, operation_id = stack
    teamwork.add_member(operation_id, "s-reader", level=OperationLevel.VIEWER, actor=OWNER)

    teamwork.assert_visible(operation_id, READER)
    with pytest.raises(NotAMemberError, match="member level"):
        teamwork.require(operation_id, READER, OperationLevel.MEMBER)


def test_membership_never_downgrades_a_global_role(stack) -> None:
    teamwork, _operations, operation_id = stack
    teamwork.add_member(operation_id, "s-guest", level=OperationLevel.VIEWER, actor=OWNER)

    # a global operator stays an operator even with a viewer row
    assert teamwork.level(operation_id, GUEST) is OperationLevel.MEMBER


def test_an_unknown_subject_cannot_be_added(stack) -> None:
    teamwork, _operations, operation_id = stack

    with pytest.raises(TeamworkError, match="has signed in"):
        teamwork.add_member(operation_id, "s-nobody", actor=OWNER)


def test_only_the_owner_adds_people(stack) -> None:
    teamwork, _operations, operation_id = stack
    teamwork.add_member(operation_id, "s-guest", actor=OWNER)

    with pytest.raises(NotAMemberError):
        teamwork.add_member(operation_id, "s-admin2", actor=GUEST)


def test_transfer_moves_ownership_and_demotes_the_old_owner(stack) -> None:
    teamwork, _operations, operation_id = stack
    teamwork.add_member(operation_id, "s-guest", actor=OWNER)

    teamwork.transfer(operation_id, "s-guest", actor=OWNER)

    assert teamwork.owner(operation_id) == "guest@x"
    assert teamwork.level(operation_id, OWNER) is OperationLevel.MEMBER


def test_the_owner_cannot_be_removed(stack) -> None:
    teamwork, _operations, operation_id = stack
    teamwork.add_member(operation_id, "s-guest", actor=OWNER)

    with pytest.raises(TeamworkError, match="owner cannot be removed"):
        teamwork.remove_member(operation_id, "s-owner", actor=OWNER)


def test_a_listing_hides_foreign_operations(stack) -> None:
    teamwork, operations, operation_id = stack
    other = operations.create("other", OperationMode.CTF_HTB, ScopeContract(includes=("y",)))
    teamwork.claim_owner(other.id, ADMIN)

    visible = teamwork.visible(operations.list_all(), GUEST)
    assert [operation.id for operation in visible] == []

    teamwork.add_member(operation_id, "s-guest", actor=OWNER)
    assert [operation.id for operation in teamwork.visible(operations.list_all(), GUEST)] == [
        operation_id
    ]


def test_a_note_keeps_its_author_and_target(stack) -> None:
    teamwork, _operations, operation_id = stack
    teamwork.add_member(operation_id, "s-guest", actor=OWNER)

    note = teamwork.note(operation_id, NoteTarget.FINDING, "fnd_1", "try  the  id  2", actor=GUEST)

    assert note.body == "try the id 2"
    assert note.author == "guest@x"
    assert [entry.id for entry in teamwork.notes(operation_id)] == [note.id]


def test_a_viewer_guest_cannot_leave_a_note(stack) -> None:
    teamwork, _operations, operation_id = stack
    teamwork.add_member(operation_id, "s-reader", level=OperationLevel.VIEWER, actor=OWNER)

    with pytest.raises(NotAMemberError):
        teamwork.note(operation_id, NoteTarget.TASK, "recon", "hi", actor=READER)


def test_an_empty_note_is_refused(stack) -> None:
    teamwork, _operations, operation_id = stack

    with pytest.raises(TeamworkError, match="needs text"):
        teamwork.note(operation_id, NoteTarget.TASK, "recon", "   ", actor=OWNER)
