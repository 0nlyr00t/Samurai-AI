"""Direction shapes and stagnation counting for the chat loop."""

from samurai_ai.tui.progress import direction


def test_prlctl_with_a_bare_remote_command_does_not_crash() -> None:
    """`prlctl exec <uuid> id` leaves no token behind the remote binary."""
    assert direction('prlctl exec "{1381d6d4-31a6-4aee-ab87-4193b12e98ae}" id') == "id"
    assert direction("prlctl exec {1381d6d4-31a6-4aee-ab87-4193b12e98ae} whoami") == "whoami"
    # and the wrapped form still sees through to the remote shell
    assert (
        direction('prlctl exec {1381d6d4-31a6-4aee} /bin/bash -c "nmap -p- 10.0.0.1"')
        == "bash nmap -p- 10.0.0.1"
    )
