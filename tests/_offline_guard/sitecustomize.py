"""Offline guard for every interpreter in the local test suite.

The directory holding this file is prepended to PYTHONPATH for spawned test
harnesses (serve, CLI, scheduler wrapper, wheel smoke), so Python imports it
automatically as `sitecustomize` at startup; conftest loads it by path for the
in-process suite. Every interpreter gets the same guarantees:

- outbound connections are refused at the Python socket layer (`connect` and
  `connect_ex`); only loopback is allowed. This is enforcement, not a timeout.
- the native HTTP transport the provider stack actually uses (yfinance ->
  curl_cffi) is neutralized at its Python entry point (`Session.request`):
  native code cannot be seen by the socket patch, so its Python-level session
  is stubbed to fail loudly instead. Importing yfinance stays legal — offline
  tests pin its adapter parameters with stubs and never let it fetch.

Honest limitation: the socket guard covers Python-level sockets and this file
stubs the one native client this project depends on. It does NOT claim to
intercept every conceivable native HTTP library; keep the test suite and its
dependencies loopback-only.
"""

import socket

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

active = False


def _check(address: object) -> None:
    if isinstance(address, tuple):
        host = address[0]
        if isinstance(host, str) and host not in _LOOPBACK:
            raise AssertionError(f"offline suite attempted an outbound connection to {address!r}")


def _block_native_transports() -> list[str]:
    """Stub the Python entry points of known native HTTP clients."""
    blocked: list[str] = []

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "native HTTP transport is blocked in the offline test suite; "
            "fixture tests must never reach the real network"
        )

    try:
        from curl_cffi import requests as native_requests

        native_requests.Session.request = refuse  # type: ignore[assignment]
        for method in ("get", "post", "head", "put", "delete", "options", "patch", "request"):
            setattr(native_requests, method, refuse)
        blocked.append("curl_cffi")
    except ImportError:
        pass
    try:
        import pycurl

        pycurl.Curl.perform = refuse  # type: ignore[assignment]
        blocked.append("pycurl")
    except ImportError:
        pass
    return blocked


def install() -> None:
    global active
    if active:
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self, address):  # type: ignore[no-untyped-def]
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        _check(address)
        return real_connect_ex(self, address)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    _block_native_transports()
    active = True


install()
