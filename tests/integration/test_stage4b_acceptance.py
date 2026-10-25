"""Stage 4b acceptance criteria as executable tests (CTF/HTB playbook).

1. A full CTF/HTB operator path works end to end: challenge brief, plan
   template, hypothesis log, flag ledger with proof, governed gates, writeup
   generation, replay and evidence export.
2. Writeups are generated only in verification/reporting from captured flags
   and never leak vault secrets.
3. The whole state survives a database reopen, and the replay lets the
   solution be repeated from the journal.
"""

import hashlib
import json
from pathlib import Path

import pytest

from samurai_ai.domain.ctf import (
    ChallengeCategory,
    CtfModeError,
    FlagFormatError,
    WriteupStateError,
)
from samurai_ai.domain.kernel import ArtifactKind
from samurai_ai.domain.operations import OperationMode, OperationStatus
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.kernel import KernelPreconditionError, KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.ctf import CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.vault import VaultRepository


class Stack:
    def __init__(self, database_path: Path) -> None:
        database = Database(database_path)
        self.operations = OperationService(OperationRepository(database))
        self.kernel = KernelService(self.operations, KernelRepository(database))
        self.vault = VaultService(
            VaultRepository(database),
            self.operations.repository,
            database_path.parent / "vault.key",
        )
        self.ctf = CtfService(
            self.operations, self.kernel, CtfRepository(database), self.vault
        )


def test_stage4b_ctf_acceptance(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    stack = Stack(database_path)
    operation = stack.operations.create(
        "Acceptance HTB box",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("10.10.10.0/24",)),
        objective="Capture user and root flags",
    )
    operation_id = operation.id

    # --- challenge init: brief, template plan, challenge-brief artifact ---
    plan = stack.ctf.init_ctf(
        operation_id,
        "AcceptanceBox",
        ChallengeCategory.PWN,
        description="vulnerable service on 10.10.10.5",
        win_condition="user.txt and root.txt",
        flag_format=r"HTB\{[a-z0-9_]+\}",
    )
    assert [task.id for task in plan.tasks] == [
        "recon",
        "hypothesis-loop",
        "exploitation",
        "flag-capture",
        "writeup",
    ]
    assert stack.kernel.kernel.artifact_exists(operation_id, "challenge-brief")

    # --- lifecycle to execution ---
    stack.kernel.transition_operation(operation_id, OperationStatus.PLANNING)
    stack.kernel.transition_operation(operation_id, OperationStatus.APPROVAL)
    stack.kernel.approve_plan(operation_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.EXECUTION)

    # --- recon and hypothesis loop with governed artifacts ---
    stack.kernel.start_task(operation_id, "recon")
    stack.kernel.complete_task(operation_id, "recon")
    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.NOTE,
        "service fingerprinted",
        requirement="recon-notes",
        task_id="hypothesis-loop",
    )
    stack.kernel.start_task(operation_id, "hypothesis-loop")

    hypothesis = stack.ctf.propose_hypothesis(
        operation_id, "Buffer overflow in name field", ChallengeCategory.PWN
    )
    dead_end = stack.ctf.propose_hypothesis(operation_id, "Format string leak")
    stack.ctf.update_hypothesis(dead_end.id, "testing")
    stack.ctf.update_hypothesis(dead_end.id, "refute", "no %n reachable")
    stack.ctf.update_hypothesis(hypothesis.id, "testing")
    stack.ctf.update_hypothesis(hypothesis.id, "confirm", "offset 72, ROP chain built")

    stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.NOTE,
        "confirmed overflow path",
        requirement="solution-path",
        task_id="exploitation",
        gate_id="solution-confirmed",
    )
    stack.kernel.complete_task(operation_id, "hypothesis-loop")
    stack.kernel.start_task(operation_id, "exploitation")

    # --- flag capture: proof artifact, format enforcement, gate artifact ---
    proof = stack.kernel.register_artifact(
        operation_id,
        ArtifactKind.COMMAND_OUTPUT,
        "./pwn && cat user.txt",
        summary="exploit run",
    )
    with pytest.raises(FlagFormatError):
        stack.ctf.record_flag(operation_id, "user", "flag{wrong-format}")
    assert stack.ctf.list_flags(operation_id) == []

    stack.vault.add("lab-token", "htb-lab-secret-42", operation_id)
    flag = stack.ctf.record_flag(
        operation_id, "user", "HTB{u53r_fl4g}", proof_artifact_id=proof.id
    )
    assert stack.kernel.kernel.artifact_exists(operation_id, "flag-proof")

    stack.kernel.complete_task(operation_id, "exploitation")
    stack.kernel.start_task(operation_id, "flag-capture")
    stack.kernel.complete_task(operation_id, "flag-capture")

    # --- verification: writeup possible, reporting blocked by gates ---
    stack.kernel.transition_operation(operation_id, OperationStatus.VERIFICATION)
    with pytest.raises(KernelPreconditionError, match="gates not passed"):
        stack.kernel.transition_operation(operation_id, OperationStatus.REPORTING)

    hypothesis_with_secret = stack.ctf.propose_hypothesis(
        operation_id, "token htb-lab-secret-42 seen in debug output"
    )
    stack.ctf.update_hypothesis(hypothesis_with_secret.id, "abandon", "not needed")

    writeup = stack.ctf.generate_writeup(operation_id)
    writeup_text = (tmp_path / writeup.path).read_text(encoding="utf-8")
    assert "AcceptanceBox" in writeup_text
    assert "HTB{u53r_fl4g}" in writeup_text
    assert "[confirmed] Buffer overflow in name field" in writeup_text
    assert "[refuted] Format string leak" in writeup_text
    assert "Solution Replay" in writeup_text
    assert "htb-lab-secret-42" not in writeup_text
    assert "***REDACTED***" in writeup_text

    stack.kernel.start_task(operation_id, "writeup")
    stack.kernel.complete_task(operation_id, "writeup")
    for gate_id in (
        "challenge-locked",
        "solution-confirmed",
        "flag-captured",
        "writeup-ready",
    ):
        stack.kernel.pass_gate(operation_id, gate_id)
    stack.kernel.transition_operation(operation_id, OperationStatus.REPORTING)

    # --- writeup export with manifest ---
    dest = tmp_path / "package"
    manifest = stack.ctf.export_writeup(operation_id, writeup.id, dest)
    assert (dest / "writeup.md").exists()
    manifest_payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest_payload["files"]:
        data = (dest / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert manifest.finding_ids == (flag.id,)

    stack.kernel.transition_operation(operation_id, OperationStatus.COMPLETE)

    # --- journal carries the full governed trail ---
    event_types = [event.event_type for event in stack.operations.events(operation_id)]
    for expected in (
        "challenge.set",
        "hypothesis.proposed",
        "hypothesis.testing",
        "hypothesis.confirmed",
        "hypothesis.refuted",
        "flag.rejected",
        "flag.captured",
        "writeup.generated",
        "writeup.exported",
    ):
        assert expected in event_types, expected

    # --- replay reproduces the solution from the journal ---
    replay = stack.ctf.replay(operation_id)
    assert replay.index("hypothesis.confirmed") < replay.index("flag.captured")
    assert "exploit run" in replay  # artifacts are listed with their hashes
    assert "sha256=" in replay

    # --- everything survives a reopen (BRD: resume after restart) ---
    reopened = Stack(database_path)
    restored = reopened.operations.get(operation_id)
    assert restored.status is OperationStatus.COMPLETE
    challenge = reopened.ctf.get_challenge(operation_id)
    assert challenge.name == "AcceptanceBox"
    assert [item.value for item in reopened.ctf.list_flags(operation_id)] == [
        "HTB{u53r_fl4g}"
    ]
    restored_writeup = reopened.ctf.ctf.get_writeup(writeup.id)
    assert restored_writeup is not None
    assert restored_writeup.sha256 == writeup.sha256
    assert "flag.captured" in reopened.ctf.replay(operation_id)


def test_stage4b_writeup_guards(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "samurai.db")
    operation = stack.operations.create(
        "Guarded lab",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("10.10.10.0/24",)),
    )
    stack.ctf.init_ctf(operation.id, "Guarded", ChallengeCategory.WEB)

    # writeup requires verification/reporting status and a captured flag
    with pytest.raises(WriteupStateError, match="verification or reporting"):
        stack.ctf.generate_writeup(operation.id)

    # ctf init is rejected on a bug bounty operation
    bounty = stack.operations.create(
        "Wrong mode",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )
    with pytest.raises(CtfModeError):
        stack.ctf.init_ctf(bounty.id, "Nope", ChallengeCategory.WEB)
