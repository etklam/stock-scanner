"""Shared offline guarantees for the whole suite (local gate only, no hosted CI).

Every in-process test runs with outbound sockets hard-blocked: only loopback
(127.0.0.1 / ::1 / unix sockets) may connect. This is enforcement, not a
timeout: an accidental Yahoo call fails the test immediately. Subprocess
harnesses are separate processes and stay offline by construction — they use
the fixture provider and loopback-only endpoints, never the default provider.
"""

import socket

import pytest

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def block_outbound_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        if isinstance(address, tuple):
            host = address[0]
            if isinstance(host, str) and host not in _LOOPBACK:
                raise AssertionError(
                    f"offline suite attempted an outbound connection to {address!r}"
                )
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
