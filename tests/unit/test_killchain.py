"""The kill-chain rail: phases per mode, the stage moving forward, the counter."""

from __future__ import annotations

from samurai_ai.domain.kernel import PlanTask, RiskClass, TaskStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.tui.killchain import KillChain, phase_for

R0 = RiskClass.R0


def test_ctf_rail_counts_flags_and_starts_at_recon() -> None:
    chain = phase_for(OperationMode.CTF_HTB, [], flag_count=1, flag_total=2)
    assert chain.phases[0] == "recon" and chain.phases[-1] == "loot"
    assert chain.current == "recon"
    assert (chain.counter_label, chain.counter_done, chain.counter_total) == ("flags", 1, 2)


def test_red_team_rail_counts_gates_and_has_no_flags() -> None:
    chain = phase_for(OperationMode.RED_TEAM, [], gate_passed=2, gate_total=5)
    assert "persistence" in chain.phases and "flags" not in chain.phases
    assert (chain.counter_label, chain.counter_done) == ("gates", 2)


def test_bug_bounty_rail_counts_verified_findings() -> None:
    chain = phase_for(OperationMode.BUG_BOUNTY, [], verified_findings=3)
    assert chain.counter_label == "verified" and chain.counter_done == 3


def test_a_done_task_title_moves_the_stage() -> None:
    tasks = [
        PlanTask(id="a", title="nmap recon", risk_class=R0, status=TaskStatus.DONE),
        PlanTask(id="b", title="privilege escalation via suid", risk_class=R0,
                 status=TaskStatus.DONE),
    ]
    chain = phase_for(OperationMode.BUG_BOUNTY, tasks)
    assert chain.current == "exploit"


def test_a_pending_task_does_not_move_the_stage() -> None:
    tasks = [PlanTask(id="a", title="privesc", risk_class=R0, status=TaskStatus.PENDING)]
    assert phase_for(OperationMode.BUG_BOUNTY, tasks).current == "recon"


def test_confirmed_facts_and_impact_levels_advance_the_stage() -> None:
    chain = phase_for(
        OperationMode.CTF_HTB,
        [],
        fact_payloads=[{"title": "reverse shell as www-data"}],
        impact_levels=["rce"],
    )
    assert chain.current in ("foothold", "privesc")


def test_the_stage_never_moves_backwards() -> None:
    tasks = [
        PlanTask(id="a", title="persistence via cron", risk_class=R0, status=TaskStatus.DONE),
        PlanTask(id="b", title="nmap scan", risk_class=R0, status=TaskStatus.DONE),
    ]
    assert phase_for(OperationMode.RED_TEAM, tasks).current == "persistence"


def test_the_counter_total_never_reads_zero_with_flags() -> None:
    assert phase_for(OperationMode.CTF_HTB, [], flag_count=1).counter_total >= 1


def test_an_empty_mode_is_falsy() -> None:
    assert not KillChain((), "", "", 0, 0)
