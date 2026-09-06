"""Shared offline guarantees for the whole suite (local gate only, no hosted CI).

The guard lives in ``tests/_offline_guard/sitecustomize.py`` so the SAME rules
apply to every interpreter: conftest installs it for the in-process suite, and
harnesses prepend its directory to PYTHONPATH so spawned interpreters (serve,
CLI, scheduler wrapper, wheel smoke) import it automatically at startup. It
refuses outbound Python-socket connects (``connect``/``connect_ex``) except to
loopback — enforcement, not a timeout — and stubs the native HTTP transports
the provider stack depends on (yfinance's curl_cffi, plus pycurl when present)
at their Python entry points, which the socket patch cannot see, so a fixture
test that strays into the real Yahoo downloader fails loudly instead of
reaching the network. The guard does NOT claim to intercept every conceivable
native HTTP library; it names and neutralizes the ones this project uses.
"""

import importlib.util
from pathlib import Path

import pytest

_GUARD = Path(__file__).parent / "_offline_guard" / "sitecustomize.py"

_spec = importlib.util.spec_from_file_location("qscan_offline_guard", _GUARD)
assert _spec is not None and _spec.loader is not None
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)


@pytest.fixture(autouse=True)
def offline_guard_installed():
    """Fail loudly if the socket guard is somehow not active for a test."""
    assert _guard.active, "offline socket guard failed to install"
    yield
