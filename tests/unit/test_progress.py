"""Stagnation: what counts as one direction, and when it fires."""

from __future__ import annotations

import pytest

from samurai_ai.tui.progress import Stagnation, direction


def test_flags_are_dropped_from_the_direction() -> None:
    assert direction("nmap -sV -p- 10.0.0.1") == "nmap 10.0.0.1"
    assert direction("nmap -p 443 10.0.0.1") == direction("nmap -sV 10.0.0.1") == "nmap 10.0.0.1"


def test_a_flag_value_is_not_mistaken_for_the_target() -> None:
    assert direction("nmap -p 443 10.0.0.1") == "nmap 10.0.0.1"
    assert direction("nuclei -u https://x -t templates") == "nuclei https://x"


def test_the_target_separates_directions() -> None:
    assert direction("curl -s https://a.example/x") != direction("curl -s https://b.example/y")


def test_a_path_to_the_binary_is_stripped() -> None:
    assert direction("/usr/bin/ffuf -w list") == "ffuf list"


def test_a_bare_binary_is_its_own_direction() -> None:
    assert direction("whoami") == "whoami"
    assert direction("   ") is None
    assert direction("") is None


def test_unbalanced_quotes_still_yield_a_direction() -> None:
    assert direction("grep 'unterminated 10.0.0.1") is not None


def test_three_repeats_fire_once() -> None:
    detector = Stagnation()

    assert detector.record("nmap -sV 10.0.0.1") is None
    assert detector.record("nmap -p- 10.0.0.1") is None
    assert detector.record("nmap -Pn 10.0.0.1") == "nmap 10.0.0.1"
    assert detector.record("nmap -O 10.0.0.1") is None  # already reported


def test_a_finding_disarms_the_streak() -> None:
    detector = Stagnation()
    for _ in range(3):
        detector.record("nmap 10.0.0.1")
    detector.finding_seen()

    assert detector.record("nmap -sV 10.0.0.1") == "nmap 10.0.0.1"


def test_a_different_direction_keeps_its_own_count() -> None:
    detector = Stagnation()

    detector.record("nmap 10.0.0.1")
    detector.record("curl https://example.com")
    assert detector.record("nmap 10.0.0.1") is None
    assert detector.record("nmap 10.0.0.1") == "nmap 10.0.0.1"  # third nmap
    detector.record("curl https://example.com")
    assert detector.record("curl https://example.com") == "curl https://example.com"


def test_the_window_forgets_an_old_streak() -> None:
    detector = Stagnation(window=4)

    detector.record("nmap 10.0.0.1")
    detector.record("nmap 10.0.0.1")
    detector.record("curl a")
    detector.record("curl b")
    assert detector.record("nmap 10.0.0.1") is None  # first nmap fell out of the window


def test_reset_clears_window_and_reports() -> None:
    detector = Stagnation()
    for _ in range(3):
        detector.record("nmap 10.0.0.1")
    detector.reset()

    assert detector.record("nmap 10.0.0.1") is None


@pytest.mark.parametrize("command", ["nmap -sV 10.0.0.1", "curl -sS https://x/api"])
def test_direction_is_stable(command: str) -> None:
    assert direction(command) == direction(command)


def test_a_relay_wrapper_is_seen_through() -> None:
    """The prlctl uuid is the same for every call, the command behind it is not."""
    vm = "{1381d6d4-31a6-4aee-ab87-4193b12e98ae}"
    assert direction(f"prlctl exec '{vm}' /bin/curl -sS http://10.0.0.1/login") == (
        "curl http://10.0.0.1/login"
    )


def test_two_endpoints_on_one_host_are_two_places() -> None:
    vm = "{1381d6d4-31a6-4aee-ab87-4193b12e98ae}"
    first = direction(f"prlctl exec '{vm}' /bin/curl http://10.0.0.1/login")
    second = direction(f"prlctl exec '{vm}' /bin/curl http://10.0.0.1/checkout")
    assert first != second


def test_prlctl_with_a_bare_remote_command_does_not_crash() -> None:
    """`prlctl exec <uuid> id` leaves no token behind the remote binary."""
    assert direction('prlctl exec "{1381d6d4-31a6-4aee-ab87-4193b12e98ae}" id') == "id"
    assert direction("prlctl exec {1381d6d4-31a6-4aee-ab87-4193b12e98ae} whoami") == "whoami"
    # and the wrapped form still sees through to the remote shell
    assert (
        direction('prlctl exec {1381d6d4-31a6-4aee} /bin/bash -c "nmap -p- 10.0.0.1"')
        == "bash nmap -p- 10.0.0.1"
    )
