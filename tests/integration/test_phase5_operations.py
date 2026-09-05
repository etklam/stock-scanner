"""Phase 5 daily-operation wrapper: both execution paths and day-skip behaviour.

The wrapper composes existing CLI/HTTP capabilities; these tests drive it as a
subprocess exactly like a scheduler would, against a real serve process (API
path) and a standalone CLI fallback (no serve), on Chinese/space paths.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, date, datetime
from pathlib import Path

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import RawPrices
from qscan.bootstrap import bootstrap
from qscan.interfaces.api.localauth import ensure_token

SESSIONS = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
CLOSES = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
CLOCK = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
SRC_ROOT = str(Path(__file__).parents[2] / "src")
WRAPPER = str(Path(__file__).parents[2] / "scripts" / "daily_scan.py")


def _prices(rows) -> RawPrices:
    return RawPrices(rows)


def seed(directory: Path) -> None:
    app = bootstrap(
        FixtureProvider({"GOOD": _prices(tuple(zip(SESSIONS, CLOSES, strict=True)))}),
        data_dir=directory,
        clock=CLOCK,
        calendar=NYSECalendar(),
    )
    try:
        app.watchlists.create("wl", ["GOOD"])
    finally:
        app.close()
    ensure_token(directory / "api-token.json")


def run_wrapper(arguments: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, WRAPPER, *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONPATH": SRC_ROOT},
    )


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


SERVE_SCRIPT = textwrap.dedent(
    """
    import sys
    from datetime import date
    from pathlib import Path

    from qscan.adapters.calendar import NYSECalendar
    from qscan.adapters.providers import FixtureProvider
    from qscan.application.contracts import RawPrices
    from qscan.interfaces.api.app import run_serve

    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)

    sessions = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    closes = CLOSES
    provider = FixtureProvider({"GOOD": RawPrices(tuple(zip(sessions, closes, strict=True)))})
    raise SystemExit(
        run_serve(
            provider,
            data_dir=Path(sys.argv[1]),
            token_path=Path(sys.argv[1]) / "api-token.json",
            port=int(sys.argv[2]),
            stop_grace=2,
            log_level="error",
        )
    )
    """
).replace("CLOSES", repr(CLOSES))


def test_daily_wrapper_uses_api_when_serve_is_up_and_skips_same_day(tmp_path):
    directory = tmp_path / "資料 daily"
    seed(directory)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    script = workdir / "serve_control.py"
    script.write_text(SERVE_SCRIPT, encoding="utf-8")
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(script), str(directory), str(port)],
        cwd=workdir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": SRC_ROOT},
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
    )
    try:
        import httpx

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("serve subprocess never became reachable")

        first = run_wrapper(
            [
                "--data-dir",
                str(directory),
                "--watchlist",
                "wl",
                "--port",
                str(port),
                "--poll-seconds",
                "1",
                "--timeout-seconds",
                "60",
            ]
        )
        assert first.returncode == 0, (first.returncode, first.stdout, first.stderr)
        state = json.loads((directory / "daily-scan-state.json").read_text())
        assert state["wl"]["state"] == "SUCCEEDED"

        # A same-day retrigger is a no-op: the state file prevents duplicates.
        second = run_wrapper(
            [
                "--data-dir",
                str(directory),
                "--watchlist",
                "wl",
                "--port",
                str(port),
                "--poll-seconds",
                "1",
                "--timeout-seconds",
                "60",
            ]
        )
        assert second.returncode == 0
        assert "nothing to do" in second.stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_daily_wrapper_falls_back_to_standalone_cli_without_serve(tmp_path):
    directory = tmp_path / "資料 standalone"
    seed(directory)
    # A qscan launcher shim so the wrapper exercises the real CLI entry point.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    if sys.platform == "win32":
        shim = shim_dir / "qscan.bat"
        shim.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" -c "from qscan.interfaces.cli import app; app()" %*\r\n'
        )
        qscan = str(shim)
    else:
        shim = shim_dir / "qscan"
        shim.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" -c "from qscan.interfaces.cli import app; app()" "$@"\n'
        )
        shim.chmod(0o755)
        qscan = str(shim)

    result = run_wrapper(
        [
            "--data-dir",
            str(directory),
            "--watchlist",
            "wl",
            "--port",
            str(free_port()),  # nothing is listening: wrapper must fall back
            "--qscan",
            qscan,
            "--timeout-seconds",
            "120",
        ]
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    state = json.loads((directory / "daily-scan-state.json").read_text())
    assert state["wl"]["state"] == "SUCCEEDED"
    assert state["wl"]["as_of_session"] == "2026-09-04"
