"""The reachability probe's pieces: address parsing and the host-side verdict."""

import socket
import threading

from samurai_ai.cli_sandbox import _is_address, _probe_host


def test_only_real_addresses_are_probed() -> None:
    assert _is_address("10.129.98.19")
    assert _is_address("127.0.0.1")
    assert not _is_address("flight.htb")
    assert not _is_address("http://localhost:3000")


def test_an_open_port_says_open() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    thread = threading.Thread(target=listener.accept, daemon=True)
    thread.start()
    try:
        assert _probe_host("127.0.0.1", port) == "open"
    finally:
        listener.close()


def test_a_closed_port_says_refused() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nothing listens any more
    assert _probe_host("127.0.0.1", port) == "refused"
