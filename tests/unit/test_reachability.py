"""The preflight asks the cheap question, and answers honestly."""

import socket
import threading

import pytest

from samurai_ai.services import reachability


def test_an_open_port_is_open() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    threading.Thread(target=listener.accept, daemon=True).start()
    try:
        assert reachability.probe_host("127.0.0.1", port) == "open"
    finally:
        listener.close()


def test_a_closed_port_is_refused() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    assert reachability.probe_host("127.0.0.1", port) == "refused"


def test_a_ping_alone_counts_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reachability, "probe_host", lambda *_: "timeout")
    monkeypatch.setattr(reachability, "pings", lambda _address: True)
    assert reachability.probe("127.0.0.1").state == "open"


def test_total_silence_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reachability, "probe_host", lambda *_: "timeout")
    monkeypatch.setattr(reachability, "pings", lambda _address: False)
    assert reachability.probe("127.0.0.1").state == "silent"


def test_an_unresolvable_name_is_reported_as_such() -> None:
    assert reachability.probe("no-such-host.invalid").state == "unresolved"


def test_the_report_says_which_targets_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"a.test": "open", "b.test": "silent"}
    monkeypatch.setattr(reachability, "resolve", lambda target: target)
    monkeypatch.setattr(
        reachability, "probe",
        lambda target: reachability.Probe(target, target, answers[target]),
    )
    report = reachability.preflight(["a.test", "b.test"])
    assert [probe.target for probe in report.answered] == ["a.test"]
    assert [probe.target for probe in report.silent] == ["b.test"]
    assert report.dead is False


def test_a_run_where_nothing_answers_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reachability, "probe",
        lambda target: reachability.Probe(target, "10.0.0.1", "silent"),
    )
    assert reachability.preflight(["10.0.0.1"]).dead is True
