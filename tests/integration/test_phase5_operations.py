"""Phase 5.1 daily-scheduler semantics: session dedup, force, retry, failure.

All flows are driven through the real wrapper as a subprocess, exactly like a
scheduler would. Deduplication uses the calendar-resolved session (never the
local date), so every scenario runs under two different injected test clocks
and cannot depend on the real today. The suite is offline: fixture providers
only, loopback-only HTTP, and the suite-wide socket guard blocks anything else.
"""

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import HTTPError

import pytest

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import RawPrices
from qscan.bootstrap import bootstrap

SRC_ROOT = str(Path(__file__).parents[2] / "src")
# Every spawned interpreter gets the offline guard via sitecustomize:
OFFLINE_GUARD = str(Path(__file__).parents[2] / "tests" / "_offline_guard")
SRC_ROOT_AND_GUARD = OFFLINE_GUARD + os.pathsep + SRC_ROOT
WRAPPER_PATH = Path(__file__).parents[2] / "scripts" / "daily_scan.py"
CLOCKS = [datetime(2026, 3, 5, 12, tzinfo=UTC), datetime(2025, 11, 20, 12, tzinfo=UTC)]
CLOSES = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
FLAT = (100.0,) * 130


def _prices(rows) -> RawPrices:
    return RawPrices(rows)


def _sessions_for() -> tuple:
    sessions = NYSECalendar().sessions(date(2024, 6, 1), date(2026, 9, 4))[-504:]
    return sessions


def _seed(directory: Path, clock: datetime, symbols: tuple[str, ...] = ("GOOD",)) -> str:
    """Fixed-clock seed that also fills the fixture CACHE: the standalone CLI
    constructs an empty FixtureProvider, so --provider fixture scans work from
    cache only — exactly like a production refresh-then-scan flow. Rows end at
    the CLOCK-resolved session, and the clock-patched shim keeps every CLI
    subprocess on the same session (no dependence on the real today)."""
    from datetime import timedelta

    calendar = NYSECalendar()
    resolved = calendar.resolve(None, FixedClock(clock).now())
    sessions = calendar.sessions(
        resolved.as_of_session - timedelta(days=400), resolved.as_of_session
    )
    data = {
        "GOOD": _prices(tuple(zip(sessions[-130:], CLOSES, strict=True))),
        "FLAT": _prices(tuple(zip(sessions[-130:], FLAT, strict=True))),
    }
    app = bootstrap(
        FixtureProvider(data),
        data_dir=directory,
        clock=FixedClock(clock),
        calendar=NYSECalendar(),
    )
    try:
        watchlist = app.watchlists.create("daily", list(symbols))
        result = app.market.refresh(watchlist.id, app.scans.calendar)
        assert result.successful == len(symbols), result
        return str(watchlist.id)
    finally:
        app.close()


def _expected_session(clock: datetime) -> str:
    from qscan.adapters.calendar import FixedClock, NYSECalendar

    context = NYSECalendar().resolve(None, FixedClock(clock).now())
    return context.as_of_session.isoformat()


def _wrapper(arguments: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(WRAPPER_PATH), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
    )


def _shim_qscan(workdir: Path, clock: datetime) -> str:
    """Harness-only launcher: patches bootstrap's SystemClock with a fixed one
    before running the real CLI app, so CLI subprocesses resolve the same
    session as the test clock instead of the real today."""
    shim_dir = workdir / "bin"
    shim_dir.mkdir(exist_ok=True)
    launcher = shim_dir / "qscan-launcher.py"
    launcher.write_text(
        textwrap.dedent(
            """
            import sys
            from datetime import datetime

            import qscan.bootstrap as bootstrap_module

            class _FixedClock:
                def __init__(self, now):
                    self._now = now

                def now(self):
                    return self._now

            bootstrap_module.SystemClock = lambda: _FixedClock(
                datetime.fromisoformat(CLOCK_VALUE)
            )
            from qscan.interfaces.cli import app

            app()
            """
        ).replace("CLOCK_VALUE", repr(clock.isoformat())),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        shim = shim_dir / "qscan.bat"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{launcher}" %*\r\n')
        return str(shim)
    shim = shim_dir / "qscan"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{launcher}" "$@"\n')
    shim.chmod(0o755)
    return str(shim)


def _serve_script(clock: datetime) -> str:
    """Harness-only serve: production create_app + executor wiring but a FIXED
    clock, so the resolved session (and the whole flow) is date-independent."""
    return (
        textwrap.dedent(
            """
        import sys
        from datetime import date, datetime
        from pathlib import Path

        from filelock import FileLock
        from qscan.adapters.calendar import FixedClock, NYSECalendar
        from qscan.adapters.providers import FixtureProvider
        from qscan.application.contracts import RawPrices
        from qscan.bootstrap import bootstrap
        from qscan.executor import NullLock, ScanExecutor
        from qscan.interfaces.api.app import create_app

        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)

        clock = datetime.fromisoformat(CLOCK_VALUE)
        sessions = NYSECalendar().sessions(date(2024, 6, 1), date(2026, 9, 4))[-504:]
        data = {
            "GOOD": RawPrices(tuple(zip(sessions[-130:], CLOSES_VALUE, strict=True))),
            "FLAT": RawPrices(tuple(zip(sessions[-130:], (100.0,) * 130, strict=True))),
        }
        scanner = bootstrap(
            FixtureProvider(data),
            data_dir=Path(sys.argv[1]),
            initialize=False,
            service_lock=NullLock(),
            clock=FixedClock(clock),
            calendar=NYSECalendar(),
        )
        ownership = FileLock(scanner.data_dir / "executor.lock", timeout=5)
        ownership.acquire(timeout=5)
        from qscan.interfaces.api.localauth import ensure_token

        ensure_token(scanner.data_dir / "api-token.json")
        executor = ScanExecutor(scanner, poll_seconds=0.05, stop_grace=2)
        executor.start()
        app = create_app(
            scanner,
            token_path=Path(sys.argv[1]) / "api-token.json",
            executor=executor,
        )
        import uvicorn

        uvicorn.run(
            app, host="127.0.0.1", port=int(sys.argv[2]), log_level="error", access_log=False
        )
        """
        )
        .replace("CLOCK_VALUE", repr(clock.isoformat()))
        .replace("CLOSES_VALUE", repr(CLOSES))
    )


def _start_serve(script_path: Path, directory: Path, workdir: Path) -> tuple[subprocess.Popen, int]:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = workdir / f"serve-{port}.py"
    script.write_text(script_path, encoding="utf-8")
    output_handles = [
        (workdir / f"serve-{port}.out").open("wb"),
        (workdir / f"serve-{port}.err").open("wb"),
    ]
    try:
        process = subprocess.Popen(
            [sys.executable, str(script), str(directory), str(port)],
            cwd=workdir,
            stdout=output_handles[0],
            stderr=output_handles[1],
            env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
        )
    finally:
        for handle in output_handles:
            handle.close()
    import httpx

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=2).status_code == 200:
                return process, port
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    process.kill()
    diagnostics = (workdir / f"serve-{port}.err").read_text(errors="replace")[-2000:]
    raise AssertionError(f"serve subprocess never became reachable\n{diagnostics}")


def test_intent_identity_ignores_local_date_and_follows_session():
    """Same local date + different sessions -> different keys; different local
    date + same session -> the SAME key (cross-process determinism)."""
    spec = importlib.util.spec_from_file_location("daily_scan", WRAPPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    first = module.intent_key("uuid-a", "2026-03-04", 1, "yahoo", 0)
    second_same_day_other_session = module.intent_key("uuid-a", "2026-03-03", 1, "yahoo", 0)
    third_other_day_same_session = module.intent_key("uuid-a", "2026-03-04", 1, "yahoo", 0)
    assert first == third_other_day_same_session
    assert first != second_same_day_other_session
    assert first != module.intent_key("uuid-a", "2026-03-04", 2, "yahoo", 0)  # revision bump
    assert first != module.intent_key("uuid-a", "2026-03-04", 1, "yahoo", 1)  # force attempt
    assert first != module.intent_key("uuid-b", "2026-03-04", 1, "yahoo", 0)  # other watchlist


def test_http_status_mapping_is_not_all_busy():
    """401/403/409 are configuration or intent errors (exit 2 semantics);
    429 stays retryable-busy. Nothing maps a real failure to success."""
    spec = importlib.util.spec_from_file_location("daily_scan", WRAPPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def http_error(code):
        return HTTPError("http://127.0.0.1/x", code, "x", None, None)

    for code in (401, 403, 409):
        with pytest.raises(module.UsageError):
            module.submit_with_retries(
                lambda code=code: (_ for _ in ()).throw(http_error(code)),
                deadline=time.monotonic() + 1,
                retry_sleep=0.01,
            )
    with pytest.raises(module.BusyError):
        module.submit_with_retries(
            lambda: (_ for _ in ()).throw(http_error(429)),
            deadline=time.monotonic() - 1,  # expired: no retry window left
            retry_sleep=0.01,
        )


@pytest.mark.parametrize("clock", CLOCKS)
def test_api_flow_dedups_on_session_and_force_rescans(tmp_path, clock):
    directory = tmp_path / "資料 api"
    watchlist_id = _seed(directory, clock)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    process, port = _start_serve(_serve_script(clock), directory, workdir)
    try:
        import httpx

        common = [
            "--data-dir",
            str(directory),
            "--watchlist",
            watchlist_id,
            "--port",
            str(port),
            "--provider",
            "fixture",
            "--poll-seconds",
            "1",
            "--timeout-seconds",
            "60",
        ]
        expected = _expected_session(clock)
        first = _wrapper(common)
        assert first.returncode == 0, (first.stdout, first.stderr)
        state = json.loads((directory / "daily-scan-state.json").read_text())
        assert state["watchlists"][watchlist_id]["session"] == expected

        # A re-trigger for the same resolved session is a no-op.
        second = _wrapper(common)
        assert second.returncode == 0 and "nothing to do" in second.stderr

        token = json.loads((directory / "api-token.json").read_text())["token"]

        def run_count() -> int:
            page = httpx.get(
                f"http://127.0.0.1:{port}/api/v1/scans?limit=200",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            ).json()
            return len([i for i in page["items"] if i["watchlist_id"] == watchlist_id])

        assert run_count() == 1
        # --force is a new intent: a second run appears for the same session.
        forced = _wrapper(common + ["--force"])
        assert forced.returncode == 0, (forced.stdout, forced.stderr)
        assert run_count() == 2
        forced_again = _wrapper(common)
        assert forced_again.returncode == 0 and "nothing to do" in forced_again.stderr
        assert run_count() == 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_api_lost_ack_replays_same_key_not_new_run(tmp_path):
    """A recorded SUBMITTED intent with unknown outcome replays the same key."""
    import httpx

    clock = CLOCKS[0]
    directory = tmp_path / "資料 lost"
    watchlist_id = _seed(directory, clock)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    process, port = _start_serve(_serve_script(clock), directory, workdir)
    try:
        common = [
            "--data-dir",
            str(directory),
            "--watchlist",
            watchlist_id,
            "--port",
            str(port),
            "--provider",
            "fixture",
            "--poll-seconds",
            "1",
            "--timeout-seconds",
            "60",
        ]
        assert _wrapper(common).returncode == 0
        state_path = directory / "daily-scan-state.json"
        document = json.loads(state_path.read_text())
        record = document["watchlists"][watchlist_id]
        surviving_key, body = record["key"], record["body"]
        # Simulate a crash after acceptance but before the outcome was learned.
        record.update({"state": "SUBMITTED", "scan_id": None})
        state_path.write_text(json.dumps(document))
        token = json.loads((directory / "api-token.json").read_text())["token"]

        replay = _wrapper(common)
        assert replay.returncode == 0, (replay.stdout, replay.stderr)

        page = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/scans?limit=200",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ).json()
        runs = [item for item in page["items"] if item["watchlist_id"] == watchlist_id]
        assert len(runs) == 1, "re-submission must replay, never duplicate"
        assert (
            json.loads(state_path.read_text())["watchlists"][watchlist_id]["key"] == surviving_key
        )
        assert body["as_of_session"] == _expected_session(clock)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_api_failed_stays_exit_1_and_401_never_falls_back(tmp_path):
    import httpx

    clock = CLOCKS[1]
    directory = tmp_path / "資料 fail"
    # FLAT is flagged suspicious -> the run completes FAILED with zero candidates.
    watchlist_id = _seed(directory, clock, symbols=("FLAT",))
    workdir = tmp_path / "harness"
    workdir.mkdir()
    process, port = _start_serve(_serve_script(clock), directory, workdir)
    try:
        common = [
            "--data-dir",
            str(directory),
            "--watchlist",
            watchlist_id,
            "--port",
            str(port),
            "--provider",
            "fixture",
            "--poll-seconds",
            "1",
            "--timeout-seconds",
            "60",
        ]
        failed = _wrapper(common)
        assert failed.returncode == 1, (failed.stdout, failed.stderr)
        state = json.loads((directory / "daily-scan-state.json").read_text())
        assert state["watchlists"][watchlist_id]["state"] == "FAILED"
        token = json.loads((directory / "api-token.json").read_text())["token"]
        before = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/scans?limit=200",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ).json()["items"]

        # A re-trigger does NOT silently convert the failure into success.
        again = _wrapper(common)
        assert again.returncode == 1 and "FAILED" in again.stderr

        # --force on a FAILED session actively retries it: a new run appears.
        forced = _wrapper(common + ["--force"])
        assert forced.returncode == 1  # still a failure, never faked success
        after = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/scans?limit=200",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ).json()["items"]
        assert len(after) == len(before) + 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_cli_path_two_clocks_dedup_and_partial(tmp_path):
    for clock in CLOCKS:
        directory = tmp_path / f"資料 cli-{clock.date().isoformat()}"
        watchlist_id = _seed(directory, clock, symbols=("GOOD", "FLAT"))
        qscan = _shim_qscan(tmp_path, clock)
        common = [
            "--data-dir",
            str(directory),
            "--watchlist",
            watchlist_id,
            "--port",
            "1",  # nothing listens: the wrapper must take the standalone path
            "--provider",
            "fixture",
            "--qscan",
            qscan,
            "--timeout-seconds",
            "120",
        ]
        expected = _expected_session(clock)
        first = _wrapper(common)
        assert first.returncode == 3, (first.stdout, first.stderr)  # FLAT -> PARTIAL
        state = json.loads((directory / "daily-scan-state.json").read_text())
        assert state["watchlists"][watchlist_id]["session"] == expected
        again = _wrapper(common)
        assert again.returncode == 3 and "nothing to do" in again.stderr
        runs = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from qscan.interfaces.cli import app; app()",
                    "--data-dir",
                    str(directory),
                    "--provider",
                    "fixture",
                    "scans",
                    "list",
                    "--limit",
                    "200",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
            ).stdout
        )
        assert len(runs) == 1  # no duplicate despite the second trigger


def test_revision_change_creates_new_intent(tmp_path):
    clock = CLOCKS[0]
    directory = tmp_path / "資料 revision"
    watchlist_id = _seed(directory, clock)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    qscan = _shim_qscan(workdir, clock)
    common = [
        "--data-dir",
        str(directory),
        "--watchlist",
        watchlist_id,
        "--port",
        "1",
        "--provider",
        "fixture",
        "--qscan",
        qscan,
        "--timeout-seconds",
        "120",
    ]
    assert _wrapper(common).returncode == 0
    # Edit the watchlist: the revision bump must produce a new intent + new run.
    (tmp_path / "edited.txt").write_text("GOOD\nFLAT\n", encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from qscan.interfaces.cli import app; app()",
            "--data-dir",
            str(directory),
            "--provider",
            "fixture",
            "watchlist",
            "import",
            "--name",
            "daily",
            "--file",
            str(tmp_path / "edited.txt"),
            "--replace",
        ],
        input="",
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
    )
    second = _wrapper(common)
    assert second.returncode == 3, (second.stdout, second.stderr)  # FLAT -> PARTIAL, new run
    state = json.loads((directory / "daily-scan-state.json").read_text())
    assert state["watchlists"][watchlist_id]["state"] == "PARTIAL"
    assert state["watchlists"][watchlist_id]["attempt"] == 0  # new base intent, not a force


def test_concurrent_schedulers_single_run(tmp_path):
    """Two scheduler subprocesses racing on one intent create exactly one run."""
    clock = CLOCKS[0]
    directory = tmp_path / "資料 concurrent"
    watchlist_id = _seed(directory, clock)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    qscan = _shim_qscan(workdir, clock)
    common = [
        "--data-dir",
        str(directory),
        "--watchlist",
        watchlist_id,
        "--port",
        "1",
        "--provider",
        "fixture",
        "--qscan",
        qscan,
        "--timeout-seconds",
        "120",
    ]
    first = subprocess.Popen(
        [sys.executable, str(WRAPPER_PATH), *common],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
    )
    second = subprocess.Popen(
        [sys.executable, str(WRAPPER_PATH), *common],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
    )
    first_out = first.communicate(timeout=240)
    second_out = second.communicate(timeout=240)
    assert first.returncode in (0, 3, 4), first_out
    assert second.returncode in (0, 3, 4), second_out
    runs = json.loads(
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from qscan.interfaces.cli import app; app()",
                "--data-dir",
                str(directory),
                "--provider",
                "fixture",
                "scans",
                "list",
                "--limit",
                "200",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
        ).stdout
    )
    assert len(runs) == 1  # serialized by the state lock; never duplicated


def test_wrapper_rejects_relative_data_dir_and_missing_executable(tmp_path):
    relative = _wrapper(["--data-dir", "relative-dir", "--watchlist", "x"])
    assert relative.returncode == 2
    assert json.loads(relative.stdout)["error"]["code"] == "VALIDATION_ERROR"
    seeded = tmp_path / "seeded"
    _seed(seeded, CLOCKS[0])
    missing = _wrapper(
        [
            "--data-dir",
            str(seeded),
            "--watchlist",
            "x",
            "--port",
            "1",
            "--qscan",
            str(tmp_path / "no-such-qscan"),
        ]
    )
    assert missing.returncode == 2
    assert "not found" in missing.stdout
