"""A loop that changes the payload is still a loop: judge by the place."""

from samurai_ai.agents.orchestrator import (
    SITE_NUDGE,
    SITE_STOP,
    SITE_WINDOW,
    SiteWindow,
    call_site,
)


def test_the_payload_does_not_change_the_site() -> None:
    a = call_site("shell.exec", {"target": "10.0.0.5", "command": "id"})
    b = call_site("shell.exec", {"target": "10.0.0.5", "command": "whoami"})
    assert a == b


def test_a_different_place_is_a_different_site() -> None:
    a = call_site("recon.httpx-probe", {"target": "10.0.0.5"})
    b = call_site("recon.httpx-probe", {"target": "10.0.0.6"})
    assert a != b


def test_a_different_tool_at_the_same_place_differs() -> None:
    assert call_site("scan.nmap", {"target": "x"}) != call_site("fuzz.ffuf", {"target": "x"})


def test_the_nudge_comes_once_at_the_threshold() -> None:
    window = SiteWindow()
    site = "shell.exec|{}"
    nudges = [window.record(site)[0] for _ in range(SITE_NUDGE)]
    assert nudges[SITE_NUDGE - 1] is True
    assert sum(nudges) == 1


def test_the_run_stops_after_the_nudge_and_more_hits() -> None:
    window = SiteWindow()
    site = "shell.exec|{}"
    stops = False
    for _ in range(SITE_STOP):
        _nudge, stop = window.record(site)
        stops = stops or stop
    assert stops


def test_a_varied_run_does_not_trip() -> None:
    window = SiteWindow()
    for index in range(SITE_WINDOW):
        assert window.record(f"tool|{index}") == (False, False)


def test_the_window_forgets_old_sites() -> None:
    window = SiteWindow()
    site = "shell.exec|{}"
    window.record(site)
    for index in range(SITE_WINDOW):
        window.record(f"other|{index}")
    nudge, stop = window.record(site)
    # the early hit fell out of the window, so this is a fresh run of one
    assert (nudge, stop) == (False, False)


def test_a_bare_shell_call_keys_on_its_signature() -> None:
    # only a command, no site key: flag-tweaked retries are one site
    a = call_site("shell.exec", {"command": "timeout 60 nxc rdp 10.0.0.5 -u x -p y"})
    b = call_site("shell.exec", {"command": "nxc --timeout 10 rdp 10.0.0.5 -u x -p y"})
    assert a == b


def test_bare_shell_calls_to_different_hosts_differ() -> None:
    a = call_site("shell.exec", {"command": "nmap -p- 10.0.0.5"})
    b = call_site("shell.exec", {"command": "nmap -p- 10.0.0.6"})
    assert a != b
