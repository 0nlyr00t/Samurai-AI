import json
from pathlib import Path

import pytest

from samurai_ai.domain.ctf import (
    ChallengeCategory,
    ChallengeExistsError,
    ChallengeNotFoundError,
    CtfModeError,
    DuplicateFlagError,
    FlagFormatError,
    HypothesisStateError,
    HypothesisStatus,
    WriteupStateError,
)
from samurai_ai.domain.kernel import ArtifactKind
from samurai_ai.domain.operations import Operation, OperationMode, OperationStatus
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.ctf import CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.vault import VaultRepository


class Stack:
    def __init__(self, database_path: Path, with_vault: bool = False) -> None:
        database = Database(database_path)
        self.operations = OperationService(OperationRepository(database))
        self.kernel = KernelService(self.operations, KernelRepository(database))
        vault = None
        if with_vault:
            vault = VaultService(
                VaultRepository(database),
                self.operations.repository,
                database_path.parent / "vault.key",
            )
        self.vault = vault
        self.ctf = CtfService(self.operations, self.kernel, CtfRepository(database), vault)


def make_operation(stack: Stack, flag_format: str = r"HTB\{[a-z0-9_]+\}") -> Operation:
    operation = stack.operations.create(
        "HTB lab",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("10.10.10.0/24",)),
    )
    stack.ctf.init_ctf(
        operation.id,
        "BabyRE",
        ChallengeCategory.REVERSE,
        description="crack the binary",
        win_condition="submit user and root flags",
        flag_format=flag_format,
    )
    return operation


def drive_to_verification(stack: Stack, operation_id: str) -> None:
    stack.kernel.transition_operation(operation_id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation_id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.EXECUTION)
    stack.kernel.transition_operation(operation_id, OperationStatus.VERIFICATION)


def record_user_flag(stack: Stack, operation_id: str) -> str:
    proof = stack.kernel.register_artifact(
        operation_id, ArtifactKind.COMMAND_OUTPUT, "./pwn --user", summary="poc run"
    )
    record = stack.ctf.record_flag(
        operation_id, "user", "HTB{u53r_fl4g}", proof_artifact_id=proof.id
    )
    return record.id


def test_init_is_one_shot_and_mode_checked(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    with pytest.raises(ChallengeExistsError):
        stack.ctf.init_ctf(operation.id, "Again", ChallengeCategory.WEB)

    bounty = stack.operations.create(
        "Bounty",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )
    with pytest.raises(CtfModeError):
        stack.ctf.init_ctf(bounty.id, "Nope", ChallengeCategory.WEB)


def test_init_creates_plan_and_challenge_brief(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    plan = stack.kernel.kernel.get_current_plan(operation.id)
    assert plan is not None
    assert [task.id for task in plan.tasks] == [
        "recon",
        "hypothesis-loop",
        "exploitation",
        "flag-capture",
        "writeup",
    ]
    assert stack.kernel.kernel.artifact_exists(operation.id, "challenge-brief")
    challenge = stack.ctf.get_challenge(operation.id)
    assert challenge.flag_format == r"HTB\{[a-z0-9_]+\}"
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "challenge.set" in events


def test_hypothesis_lifecycle(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    hypothesis = stack.ctf.propose_hypothesis(
        operation.id, "Stack smash via oversized name", ChallengeCategory.PWN
    )
    assert hypothesis.status is HypothesisStatus.OPEN

    with pytest.raises(HypothesisStateError):
        stack.ctf.update_hypothesis(hypothesis.id, "confirm")

    stack.ctf.update_hypothesis(hypothesis.id, "testing")
    updated = stack.ctf.update_hypothesis(hypothesis.id, "confirm", "offset 72, ROP works")
    assert updated.status is HypothesisStatus.CONFIRMED
    assert updated.notes == "offset 72, ROP works"
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "hypothesis.proposed" in events
    assert "hypothesis.testing" in events
    assert "hypothesis.confirmed" in events


def test_flag_format_rejected_and_not_recorded(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    with pytest.raises(FlagFormatError):
        stack.ctf.record_flag(operation.id, "user", "flag{wrong}")
    assert stack.ctf.list_flags(operation.id) == []
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "flag.rejected" in events


def test_duplicate_flag_rejected(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    record_user_flag(stack, operation.id)
    with pytest.raises(DuplicateFlagError):
        stack.ctf.record_flag(operation.id, "user", "HTB{an0ther}")


def test_flag_requires_challenge_and_valid_proof(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = stack.operations.create(
        "No challenge",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("10.10.10.0/24",)),
    )
    with pytest.raises(ChallengeNotFoundError):
        stack.ctf.record_flag(operation.id, "user", "HTB{u53r}")

    ready = make_operation(stack)
    with pytest.raises(Exception, match="proof artifact"):
        stack.ctf.record_flag(
            ready.id, "user", "HTB{u53r}", proof_artifact_id="art_missing"
        )


def test_record_flag_registers_gate_artifact(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    record_user_flag(stack, operation.id)
    assert stack.kernel.kernel.artifact_exists(operation.id, "flag-proof")
    artifacts = stack.kernel.kernel.list_artifacts(operation.id)
    proof = next(item for item in artifacts if item.requirement == "flag-proof")
    assert proof.gate_id == "flag-captured"
    assert proof.task_id == "flag-capture"


def test_writeup_requires_verification_status(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    record_user_flag(stack, operation.id)
    with pytest.raises(WriteupStateError, match="verification or reporting"):
        stack.ctf.generate_writeup(operation.id)


def test_writeup_requires_captured_flag(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    drive_to_verification(stack, operation.id)
    with pytest.raises(WriteupStateError, match="no captured flags"):
        stack.ctf.generate_writeup(operation.id)


def test_generate_writeup_markdown(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    hypothesis = stack.ctf.propose_hypothesis(operation.id, "BOF in name field")
    stack.ctf.update_hypothesis(hypothesis.id, "testing")
    stack.ctf.update_hypothesis(hypothesis.id, "confirm")
    flag_id = record_user_flag(stack, operation.id)
    drive_to_verification(stack, operation.id)

    writeup = stack.ctf.generate_writeup(operation.id)
    text = (tmp_path / writeup.path).read_text(encoding="utf-8")
    assert "BabyRE" in text
    assert "HTB{u53r_fl4g}" in text
    assert "[confirmed] BOF in name field" in text
    assert "Solution Replay" in text
    assert writeup.flag_ids == (flag_id,)
    assert stack.kernel.kernel.artifact_exists(operation.id, "final-writeup")
    events = [event.event_type for event in stack.operations.events(operation.id)]
    assert "writeup.generated" in events


def test_generate_writeup_json(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    record_user_flag(stack, operation.id)
    drive_to_verification(stack, operation.id)

    writeup = stack.ctf.generate_writeup(operation.id, ReportFormat.JSON)
    payload = json.loads((tmp_path / writeup.path).read_text(encoding="utf-8"))
    assert payload["challenge"]["name"] == "BabyRE"
    assert payload["flags"][0]["value"] == "HTB{u53r_fl4g}"
    assert "flag.captured" in payload["replay"]


def test_writeup_redacts_vault_secrets(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db", with_vault=True)
    assert stack.vault is not None
    operation = make_operation(stack)
    stack.vault.add("lab-token", "htb-token-secret-777", operation.id)
    hypothesis = stack.ctf.propose_hypothesis(
        operation.id, "Token htb-token-secret-777 leaks via debug page"
    )
    stack.ctf.update_hypothesis(hypothesis.id, "testing")
    stack.ctf.update_hypothesis(hypothesis.id, "confirm", "used htb-token-secret-777")
    record_user_flag(stack, operation.id)
    drive_to_verification(stack, operation.id)

    writeup = stack.ctf.generate_writeup(operation.id)
    text = (tmp_path / writeup.path).read_text(encoding="utf-8")
    assert "htb-token-secret-777" not in text
    assert "***REDACTED***" in text


def test_replay_orders_events_chronologically(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    hypothesis = stack.ctf.propose_hypothesis(operation.id, "First idea")
    stack.ctf.update_hypothesis(hypothesis.id, "testing")
    record_user_flag(stack, operation.id)

    replay = stack.ctf.replay(operation.id)
    proposed = replay.index("hypothesis.proposed")
    testing = replay.index("hypothesis.testing")
    captured = replay.index("flag.captured")
    assert proposed < testing < captured
    assert "sha256=" in replay


def test_export_writeup_manifest(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    record_user_flag(stack, operation.id)
    drive_to_verification(stack, operation.id)
    writeup = stack.ctf.generate_writeup(operation.id)

    dest = tmp_path / "export"
    manifest = stack.ctf.export_writeup(operation.id, writeup.id, dest)
    assert (dest / "writeup.md").exists()
    assert (dest / "manifest.json").exists()
    paths = {entry.path for entry in manifest.files}
    assert "writeup.md" in paths
    assert any(path.startswith("artifacts/") for path in paths)
    payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert payload["report_id"] == writeup.id


def test_summary_counts(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = make_operation(stack)
    first = stack.ctf.propose_hypothesis(operation.id, "One")
    stack.ctf.propose_hypothesis(operation.id, "Two")
    stack.ctf.update_hypothesis(first.id, "testing")
    stack.ctf.update_hypothesis(first.id, "refute", "nope")
    record_user_flag(stack, operation.id)

    summary = stack.ctf.summary(operation.id)
    assert summary.hypotheses_open == 1
    assert summary.hypotheses_refuted == 1
    assert summary.hypotheses_confirmed == 0
    assert summary.flags_captured == 1
    assert summary.writeups == 0
