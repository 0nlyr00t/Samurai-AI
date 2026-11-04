"""Destination parsing and the ssh command line the tunnel builds."""

import pytest

from samurai_ai.services.ssh_tunnel import Destination, SshTunnel, TunnelError


def test_a_plain_host_parses() -> None:
    target = Destination.parse("box.example.com")
    assert target.host == "box.example.com" and target.user == "" and target.port == 0
    assert target.ssh_target() == "box.example.com"


def test_user_and_port_parse() -> None:
    target = Destination.parse("root@10.10.14.7:2222")
    assert (target.user, target.host, target.port) == ("root", "10.10.14.7", 2222)
    assert target.ssh_target() == "root@10.10.14.7"


def test_an_empty_destination_is_refused() -> None:
    with pytest.raises(TunnelError, match="empty destination"):
        Destination.parse("  ")


def test_a_missing_host_is_refused() -> None:
    with pytest.raises(TunnelError, match="cannot read a host"):
        Destination.parse(":2222")


def test_the_command_forwards_a_socket_and_keeps_the_forward_alive(tmp_path) -> None:
    tunnel = SshTunnel(Destination.parse("root@box:2222"), tmp_path / "s.sock")
    argv = tunnel.argv()
    assert argv[0] == "ssh"
    assert "-N" in argv and "ExitOnForwardFailure=yes" in argv
    assert f"{tmp_path / 's.sock'}:$HOME/.config/samurai/run/serve.sock" in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[-1] == "root@box"


def test_extra_ssh_arguments_pass_through(tmp_path) -> None:
    tunnel = SshTunnel(
        Destination.parse("box"),
        tmp_path / "s.sock",
        remote_socket="/run/x.sock",
        extra_args=("-o", "StrictHostKeyChecking=accept-new"),
    )
    argv = tunnel.argv()
    assert argv[-1] == "box"
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[argv.index("-L") + 1].endswith(":/run/x.sock")
