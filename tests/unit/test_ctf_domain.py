from __future__ import annotations

import pytest
from pydantic import ValidationError

from samurai_ai.domain.ctf import (
    ChallengeCategory,
    CtfChallenge,
    FlagRecord,
    Hypothesis,
    HypothesisStateError,
    HypothesisStatus,
    ctf_template,
    flag_matches_format,
)


def test_challenge_categories_cover_brd_submodes() -> None:
    assert {item.value for item in ChallengeCategory} == {
        "web",
        "pwn",
        "reverse",
        "crypto",
        "forensics",
        "misc",
        "privesc",
    }


def test_challenge_accepts_valid_flag_format() -> None:
    challenge = CtfChallenge(
        operation_id="op1",
        name="BabyRE",
        category=ChallengeCategory.REVERSE,
        flag_format=r"HTB\{[a-z0-9_]+\}",
    )
    assert challenge.accepts_flag("HTB{w3ll_d0ne}")
    assert not challenge.accepts_flag("flag{nope}")


def test_challenge_without_format_accepts_any_flag() -> None:
    challenge = CtfChallenge(
        operation_id="op1", name="Misc", category=ChallengeCategory.MISC
    )
    assert challenge.accepts_flag("anything goes")


def test_challenge_rejects_invalid_regex() -> None:
    with pytest.raises(ValidationError):
        CtfChallenge(
            operation_id="op1",
            name="Broken",
            category=ChallengeCategory.WEB,
            flag_format="HTB{[",
        )


def test_flag_matches_format_empty_pattern() -> None:
    assert flag_matches_format("value", "")
    assert flag_matches_format("abc", r"a.c")
    assert not flag_matches_format("xabc", r"a.c")


def test_hypothesis_happy_path_open_testing_confirmed() -> None:
    hypothesis = Hypothesis(operation_id="op1", title="SQLi in login")
    assert hypothesis.status is HypothesisStatus.OPEN
    hypothesis.begin_testing()
    assert hypothesis.status is HypothesisStatus.TESTING
    hypothesis.confirm("confirmed via union select")
    assert hypothesis.status is HypothesisStatus.CONFIRMED
    assert hypothesis.notes == "confirmed via union select"
    assert hypothesis.version == 3


def test_hypothesis_refute_and_abandon_paths() -> None:
    hypothesis = Hypothesis(operation_id="op1", title="LFI via params")
    hypothesis.begin_testing()
    hypothesis.refute("params are filtered")
    assert hypothesis.status is HypothesisStatus.REFUTED

    other = Hypothesis(operation_id="op1", title="Dead end")
    other.abandon("not applicable")
    assert other.status is HypothesisStatus.ABANDONED


def test_hypothesis_invalid_transitions_raise() -> None:
    hypothesis = Hypothesis(operation_id="op1", title="Untested")
    with pytest.raises(HypothesisStateError):
        hypothesis.confirm()
    with pytest.raises(HypothesisStateError):
        hypothesis.refute()

    hypothesis.begin_testing()
    with pytest.raises(HypothesisStateError):
        hypothesis.begin_testing()
    hypothesis.confirm()
    with pytest.raises(HypothesisStateError):
        hypothesis.abandon()


def test_flag_record_is_frozen() -> None:
    record = FlagRecord(operation_id="op1", name="user", value="HTB{u53r}")
    with pytest.raises(ValidationError):
        record.value = "changed"  # type: ignore[misc]


def test_ctf_template_structure() -> None:
    template = ctf_template()
    assert template.name == "ctf-htb"
    task_ids = [task.id for task in template.tasks]
    assert task_ids == [
        "recon",
        "hypothesis-loop",
        "exploitation",
        "flag-capture",
        "writeup",
    ]
    gate_ids = [gate.id for gate in template.gates]
    assert gate_ids == [
        "challenge-locked",
        "solution-confirmed",
        "flag-captured",
        "writeup-ready",
    ]
    flag_task = template.tasks[3]
    assert "flag-proof" in flag_task.required_artifacts
    writeup_gate = template.gates[3]
    assert "final-writeup" in writeup_gate.required_artifacts
