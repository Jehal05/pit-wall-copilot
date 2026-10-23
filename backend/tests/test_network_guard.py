"""Local-demo-fixed repair, issue 5: proves the autouse network guard in
`conftest.py` (`_block_external_network_access`) is not merely present but
actually effective — it must reject a real attempt to resolve or connect to
an external host, while continuing to allow loopback traffic (which the
rest of the suite, e.g. `tests/test_websocket_events.py`'s in-process ASGI
WebSocket tests, already exercises and which the full-suite run in this
same pass proves keeps working).

These tests intentionally do NOT depend on any real network being reachable
or unreachable in the environment that runs them: they assert that the
guard itself raises before any real DNS/socket work would happen, so they
pass identically online, offline, air-gapped, or in CI.
"""

from __future__ import annotations

import socket

import pytest

from tests.conftest import ExternalNetworkAccessBlocked


def test_dns_lookup_for_an_external_host_is_blocked():
    with pytest.raises(ExternalNetworkAccessBlocked):
        socket.getaddrinfo("example.com", 443)


def test_dns_lookup_for_loopback_hosts_is_not_blocked():
    # These may still raise ordinary socket errors depending on the
    # sandbox's own DNS/hosts setup, but they must never raise
    # ExternalNetworkAccessBlocked - the guard itself must not be what's
    # stopping loopback resolution.
    for host in ("127.0.0.1", "localhost", "::1"):
        try:
            socket.getaddrinfo(host, 80)
        except ExternalNetworkAccessBlocked:
            raise
        except OSError:
            pass  # unrelated environment-specific resolution failure is fine


def test_socket_connect_to_an_external_host_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ExternalNetworkAccessBlocked):
            sock.connect(("93.184.216.34", 80))  # a real, non-loopback IP; never actually dialed
    finally:
        sock.close()


def test_socket_connect_to_loopback_is_not_blocked_by_the_guard():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(Exception) as exc_info:
            # Nothing is listening on this port, so this still fails - but
            # it must fail with an ordinary connection error, never with
            # ExternalNetworkAccessBlocked, proving loopback is exempted
            # from the guard rather than merely happening to succeed.
            sock.connect(("127.0.0.1", 1))
        assert not isinstance(exc_info.value, ExternalNetworkAccessBlocked)
    finally:
        sock.close()
