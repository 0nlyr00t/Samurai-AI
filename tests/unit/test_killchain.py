"""The kill-chain rail: phases per mode, the stage moving forward, the counter."""

from __future__ import annotations

from pathlib import Path

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


def test_access_rows_take_hosts_from_facts_with_the_strongest_level() -> None:
    from samurai_ai.tui.killchain import access_rows

    rows = access_rows(
        [
            "banner 10.0.0.5 ssh OpenSSH 9.2",
            "shell as www-data on 10.0.0.5 via forged qr",
            "root on 10.0.0.5 through suid bash",
            "10.0.0.9 http 200, app.htb serves nginx",
        ]
    )
    by_host = {row.host: row.level for row in rows}
    assert by_host["10.0.0.5"] == "root"
    assert by_host["10.0.0.9"] == "read"
    assert by_host["app.htb"] == "read"


def test_access_rows_ignore_facts_without_hosts() -> None:
    from samurai_ai.tui.killchain import access_rows

    assert access_rows(["nothing useful here", ""]) == []


def test_access_rows_skip_out_of_scope_mentions() -> None:
    """A gateway or resolver quoted by a fact is not access — scope decides."""
    from samurai_ai.domain.scope import ScopeContract
    from samurai_ai.tui.killchain import access_rows

    facts = [
        "IP цели верный: HTB API отдаёт DutyBound = 10.129.138.134, "
        "lab-гейт 10.8.1.1, dns 1.1.1.1"
    ]
    scope = ScopeContract(includes=("10.129.138.134",))
    rows = access_rows(facts, scope)
    assert [row.host for row in rows] == ["10.129.138.134"]
    # and without a scope the old behaviour stays
    assert len(access_rows(facts)) == 3


def test_open_intents_move_the_stage_too() -> None:
    """The chat path writes its work as graph intents; the rail must see them."""
    chain = phase_for(
        OperationMode.CTF_HTB,
        [],
        intent_texts=["Собрать SSTI payload для заметок билета"],
    )
    assert chain.current == "foothold"
    # and without them the same operation is still at recon
    assert phase_for(OperationMode.CTF_HTB, []).current == "recon"


def test_ctf_counter_reads_flags_and_not_verified_findings() -> None:
    """The counter follows the mode: flags only exist in CTF."""
    ctf = phase_for(OperationMode.CTF_HTB, [], flag_count=2, flag_total=2, verified_findings=9)
    bounty = phase_for(OperationMode.BUG_BOUNTY, [], verified_findings=9)
    assert (ctf.counter_label, ctf.counter_done) == ("flags", 2)
    assert (bounty.counter_label, bounty.counter_done) == ("verified", 9)


def test_scope_rows_mark_touched_targets_and_hide_exclusions() -> None:
    from samurai_ai.domain.scope import ScopeContract
    from samurai_ai.tui.killchain import scope_rows

    scope = ScopeContract(
        includes=("10.0.0.0/24", "*.example.com"),
        excludes=("10.0.0.1",),
        hosts={"flight.htb": "10.0.0.7"},
    )
    rows = scope_rows(scope, {"10.0.0.7", "other.htb"})
    by_rule = {row.rule: row for row in rows}
    # a touched host inside the network marks the include rule
    assert by_rule["10.0.0.0/24"].hit is True
    assert by_rule["*.example.com"].hit is False
    # the alias is hit through the address it stands for, with the note
    assert by_rule["flight.htb"].hit is True
    assert by_rule["flight.htb"].note == "→ 10.0.0.7"
    # excluded targets disappear from TARGETS instead of looking active
    assert "10.0.0.1" not in by_rule

    # a duplicate include/exclusion pair is hidden by the exclusion too
    duplicated = scope_rows(
        ScopeContract(
            includes=("scaffold.htb", "10.129.246.248"),
            excludes=("scaffold.htb", "10.129.246.248"),
        ),
        {"scaffold.htb", "10.129.246.248"},
    )
    assert duplicated == []


def test_scope_rows_resolve_a_touched_alias_into_its_rule() -> None:
    from samurai_ai.domain.scope import ScopeContract
    from samurai_ai.tui.killchain import scope_rows

    scope = ScopeContract(includes=("10.0.0.9",), hosts={"flight.htb": "10.0.0.9"})
    rows = scope_rows(scope, {"flight.htb"})
    by_rule = {row.rule: row for row in rows}
    assert by_rule["flight.htb"].hit is True
    # the alias name touched in a command also marks the rule its address means
    assert by_rule["10.0.0.9"].hit is True


def test_the_marker_table_covers_every_fallback_phase() -> None:
    """Each chain phase must be reachable by some marker, or the rail stalls."""
    from samurai_ai.tui.killchain import _CHAINS, _MARKERS

    reachable = {phase for _, candidates in _MARKERS for phase in candidates}
    for mode, chain in _CHAINS.items():
        missing = set(chain[1:]) - reachable
        assert not missing, f"{mode} has unreachable stages: {missing}"



def _chain(current: str = "recon") -> object:
    from samurai_ai.tui.killchain import KillChain

    return KillChain(("recon", "foothold", "privesc"), current, "flags", 0, 2)


def _task(title: str, status: str, required: tuple[str, ...] = ()) -> object:
    from samurai_ai.domain.kernel import PlanTask

    return PlanTask(id=title.split()[0], title=title, status=status, required_artifacts=required)


def test_a_phase_is_complete_only_behind_the_current_one() -> None:
    from samurai_ai.tui.killchain import PhaseState, phase_views

    views = phase_views(_chain("foothold"), [_task("recon scan", "done")])  # type: ignore[arg-type]
    assert [view.state for view in views] == [
        PhaseState.COMPLETE,
        PhaseState.READY,
        PhaseState.NOT_STARTED,
    ]


def test_a_blocked_task_blocks_the_phase_and_names_itself() -> None:
    from samurai_ai.tui.killchain import PhaseState, phase_views

    views = phase_views(
        _chain("foothold"),  # type: ignore[arg-type]
        [_task("foothold shell access", "blocked")],
    )
    assert views[1].state is PhaseState.BLOCKED
    assert views[1].reason == "foothold shell access"


def test_a_failed_gate_blocks_the_phase_it_names() -> None:
    from samurai_ai.tui.killchain import PhaseState, phase_views

    views = phase_views(
        _chain("recon"),  # type: ignore[arg-type]
        [_task("recon scan", "done")],
        gates=[("recon gate", "failed")],
    )
    assert views[0].state is PhaseState.BLOCKED
    assert views[0].reason == "recon gate"


def test_a_missing_artifact_is_an_unmet_criterion_not_a_guess() -> None:
    from samurai_ai.tui.killchain import PhaseState, phase_views

    task = _task("recon scan", "done", ("scan-output",))
    blocked = phase_views(_chain("recon"), [task], artifacts=[])  # type: ignore[arg-type]
    assert blocked[0].state is PhaseState.BLOCKED
    assert blocked[0].missing == ("scan-output",)
    ready = phase_views(_chain("recon"), [task], artifacts=["scan-output"])  # type: ignore[arg-type]
    assert ready[0].state is PhaseState.READY


def test_an_override_needs_a_known_phase_and_a_reason(tmp_path: object) -> None:
    from samurai_ai.domain.operations import OperationMode, ScopeContract
    from samurai_ai.services.kernel import KernelService
    from samurai_ai.services.killchain import KillchainService, PhaseOverrideError
    from samurai_ai.services.operations import OperationService
    from samurai_ai.storage.database import Database
    from samurai_ai.storage.kernel import KernelRepository
    from samurai_ai.storage.operations import OperationRepository

    database = Database(Path(str(tmp_path)) / "k.sqlite")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    service = KillchainService(operations, kernel)
    operation = operations.create("K", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.1",)))

    service.override(operation.id, "loot", "flag captured by hand")
    assert service.active(operation.id) == ("loot", "flag captured by hand")
    service.clear(operation.id)
    assert service.active(operation.id) is None

    try:
        service.override(operation.id, "impact", "wrong chain")
    except PhaseOverrideError as exc:
        assert "impact" in str(exc)
    else:  # pragma: no cover - the chain has no impact phase in CTF
        raise AssertionError("an unknown phase must be refused")

    try:
        service.override(operation.id, "loot", "   ")
    except PhaseOverrideError as exc:
        assert "reason" in str(exc)
    else:  # pragma: no cover - an override without a reason is noise
        raise AssertionError("a reasonless override must be refused")


def test_coverage_distinguishes_covered_pending_and_out_of_scope() -> None:
    from samurai_ai.tui.coverage import CoverageState, summarise, surface_rows

    rows = surface_rows(("a.example.com", "b.example.com", "c.example.com"),
                        touched=["a.example.com"], excluded=["c.example.com"])
    assert [row.state for row in rows] == [
        CoverageState.COVERED,
        CoverageState.PENDING,
        CoverageState.BLOCKED_SCOPE,
    ]
    assert summarise(rows) == (1, 3)


def test_a_blocked_check_class_carries_its_task_and_an_unasked_one_is_na() -> None:
    from samurai_ai.tui.coverage import CoverageState, check_rows

    rows = check_rows(
        [
            PlanTask(id="brute-1", title="brute force the login", status=TaskStatus.BLOCKED),
            PlanTask(id="fuzz-1", title="fuzz paths", status=TaskStatus.DONE),
        ]
    )
    states = {row.target: row.state for row in rows}
    assert states["brute"] is CoverageState.BLOCKED_POLICY
    assert states["fuzz"] is CoverageState.COVERED
    assert states["spray"] is CoverageState.NOT_APPLICABLE
    blocked = next(row for row in rows if row.target == "brute")
    assert blocked.reason == "brute force the login"
