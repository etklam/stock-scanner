"""Phase 6A regressions: pending-intent recovery and the wrapper error contract.

Every scenario drives the real wrapper as a subprocess against the real serve
harness (fixture provider, fixed clocks, loopback only). The behaviors pinned
here failed under the Phase 5.1 wrapper, which rebuilt intents from today's
session/revision before checking what was already submitted.
"""

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
from test_phase5_operations import (
    CLOCKS,
    SRC_ROOT_AND_GUARD,
    WRAPPER_PATH,
    _expected_session,
    _seed,
    _serve_script,
    _start_serve,
    _wrapper,
)

STATE = "daily-scan-state.json"

_SPAN_SERVE = textwrap.dedent(
    """
    import sys
    import time
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
    closes = (CLOSES_VALUE * 4)[:504]
    data = {"GOOD": RawPrices(tuple(zip(sessions, closes, strict=True)))}

    class _Park(FixtureProvider):
        # Harness only: fetch parks while the hold file exists (never created
        # by tests that do not need it, so the park stays inert).
        def fetch(self, instrument, start, end):
            hold = Path(sys.argv[1]) / "hold"
            deadline = time.monotonic() + 120
            while hold.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            return super().fetch(instrument, start, end)

    scanner = bootstrap(
        _Park(data),
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
    app = create_app(scanner, token_path=Path(sys.argv[1]) / "api-token.json", executor=executor)
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="error", access_log=False)
    """
)


def _span_serve_script(clock) -> str:
    """Serve harness whose fixture data spans the FULL 504-session window, so
    scans resolve for BOTH test clocks (the shared harness only carries the
    tail 130 sessions of one clock). Fetch parks while <data-dir>/hold exists,
    which only the poll-timeout test creates."""
    from test_phase5_operations import CLOSES

    return _SPAN_SERVE.replace("CLOCK_VALUE", repr(clock.isoformat())).replace(
        "CLOSES_VALUE", repr(CLOSES)
    )


def _seed_list_only(directory, clock) -> str:
    """Create the watchlist WITHOUT filling the cache, so a scan must fetch
    (and can be held) on the serve path."""
    from qscan.adapters.calendar import FixedClock, NYSECalendar
    from qscan.adapters.providers import FixtureProvider
    from qscan.bootstrap import bootstrap

    app = bootstrap(
        FixtureProvider({}),
        data_dir=directory,
        clock=FixedClock(clock),
        calendar=NYSECalendar(),
    )
    try:
        return str(app.watchlists.create("daily", ["GOOD"]).id)
    finally:
        app.close()


def _load_module():
    spec = importlib.util.spec_from_file_location("daily_scan", WRAPPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_pending(
    directory: Path, watchlist_id: str, session: str, *, scan_id: str | None, attempt: int = 0
):
    """Record a crashed submission whose outcome is unknown (pending intent)."""
    module = _load_module()
    key = module.intent_key(watchlist_id, session, 1, "fixture", attempt)
    document = {
        "version": 1,
        "watchlists": {
            watchlist_id: {
                "watchlist_name": "daily",
                "session": session,
                "revision": 1,
                "provider": "fixture",
                "attempt": attempt,
                "key": key,
                "body": {
                    "watchlist_id": watchlist_id,
                    "as_of_session": session,
                    "data_mode": "auto",
                },
                "scan_id": scan_id,
                "state": "SUBMITTED",
            }
        },
    }
    (directory / STATE).write_text(json.dumps(document, indent=2), encoding="utf-8")
    return key


def _runs_for(base: str, token: str, watchlist_id: str) -> list[dict]:
    page = httpx.get(
        f"{base}/api/v1/scans?limit=200",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ).json()
    return [item for item in page["items"] if item["watchlist_id"] == watchlist_id]


def _record(directory: Path, watchlist_id: str) -> dict:
    return json.loads((directory / STATE).read_text())["watchlists"][watchlist_id]


def test_old_session_pending_resumes_then_new_session_intent(tmp_path):
    """Regressions 1+6: a SUBMITTED intent for an older session resumes with its
    original key/scan id even though the restarted server resolves a NEW
    session; afterwards today's session is handled normally as a new intent."""
    clock_a, clock_b = CLOCKS
    directory = tmp_path / "資料 pending-old"
    watchlist_id = _seed(directory, clock_a)
    workdir = tmp_path / "harness-a"
    workdir.mkdir()
    process, port = _start_serve(_serve_script(clock_a), directory, workdir)
    try:
        arguments = [
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
        assert _wrapper(arguments).returncode == 0
        old_key = _record(directory, watchlist_id)["key"]
        # Crash after the server accepted, before the outcome was learned.
        document = json.loads((directory / STATE).read_text())
        document["watchlists"][watchlist_id]["state"] = "SUBMITTED"
        (directory / STATE).write_text(json.dumps(document))
    finally:
        process.kill()
        process.wait(10)

    workdir_b = tmp_path / "harness-b"
    workdir_b.mkdir()
    process_b, port_b = _start_serve(_span_serve_script(clock_b), directory, workdir_b)
    try:
        arguments = [
            "--data-dir",
            str(directory),
            "--watchlist",
            watchlist_id,
            "--port",
            str(port_b),
            "--provider",
            "fixture",
            "--poll-seconds",
            "1",
            "--timeout-seconds",
            "60",
        ]
        resumed = _wrapper(arguments)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert "resuming pending intent" in resumed.stderr
        token = json.loads((directory / "api-token.json").read_text())["token"]
        runs = _runs_for(f"http://127.0.0.1:{port_b}", token, watchlist_id)
        assert len(runs) == 2, "old intent must replay; today must be one new intent"
        assert {run["as_of_session"] for run in runs} == {
            _expected_session(clock_a),
            _expected_session(clock_b),
        }
        final = _record(directory, watchlist_id)
        assert final["state"] == "SUCCEEDED"
        assert final["session"] == _expected_session(clock_b)
        assert final["attempt"] == 0  # a new base intent, not an accidental force
        assert final["key"] != old_key
    finally:
        if process_b.poll() is None:
            process_b.kill()
            process_b.wait(10)


def test_force_intent_pending_keeps_attempt_on_plain_retry(tmp_path):
    """Regression 2: an unfinished --force intent (attempt 1) is resumed with
    its attempt-1 key by a PLAIN retry; falling back to attempt 0 would orphan
    that intent and create a second run under a different key."""
    clock = CLOCKS[0]
    directory = tmp_path / "資料 force-pending"
    watchlist_id = _seed(directory, clock)
    session = _expected_session(clock)
    key = _write_pending(directory, watchlist_id, session, scan_id=None, attempt=1)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    process, port = _start_serve(_serve_script(clock), directory, workdir)
    try:
        arguments = [
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
        plain_retry = _wrapper(arguments)
        assert plain_retry.returncode == 0, (plain_retry.stdout, plain_retry.stderr)
        token = json.loads((directory / "api-token.json").read_text())["token"]
        runs = _runs_for(f"http://127.0.0.1:{port}", token, watchlist_id)
        assert len(runs) == 1, "the attempt-1 key must be reused, never a second attempt-0 run"
        final = _record(directory, watchlist_id)
        assert final["key"] == key and final["attempt"] == 1
        assert final["state"] == "SUCCEEDED"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_lost_ack_survives_rename_and_revision_bump_across_dates(tmp_path):
    """Regression 3: an accepted-but-unacknowledged submission is recovered by
    its saved scan id even after the list was renamed (revision bumped) and the
    resolved session moved on."""
    clock_a, clock_b = CLOCKS
    directory = tmp_path / "資料 rename"
    watchlist_id = _seed(directory, clock_a)
    workdir = tmp_path / "harness-a"
    workdir.mkdir()
    process, port = _start_serve(_serve_script(clock_a), directory, workdir)
    try:
        token = json.loads((directory / "api-token.json").read_text())["token"]
        arguments = [
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
        assert _wrapper(arguments).returncode == 0
        # Rename the list (revision bump) while the intent stays pending.
        response = httpx.patch(
            f"http://127.0.0.1:{port}/api/v1/watchlists/{watchlist_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "daily-renamed", "expected_revision": 1},
            timeout=10,
        )
        assert response.status_code == 200, response.text
        document = json.loads((directory / STATE).read_text())
        document["watchlists"][watchlist_id]["state"] = "SUBMITTED"
        (directory / STATE).write_text(json.dumps(document))
    finally:
        process.kill()
        process.wait(10)

    workdir_b = tmp_path / "harness-b"
    workdir_b.mkdir()
    process_b, port_b = _start_serve(_span_serve_script(clock_b), directory, workdir_b)
    try:
        arguments = [
            "--data-dir",
            str(directory),
            "--watchlist",
            "daily-renamed",  # the OLD name in the pending record no longer exists
            "--port",
            str(port_b),
            "--provider",
            "fixture",
            "--poll-seconds",
            "1",
            "--timeout-seconds",
            "60",
        ]
        resumed = _wrapper(arguments)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert "resuming pending intent" in resumed.stderr
        token = json.loads((directory / "api-token.json").read_text())["token"]
        runs = _runs_for(f"http://127.0.0.1:{port_b}", token, watchlist_id)
        assert len(runs) == 2
        assert {run["as_of_session"] for run in runs} == {
            _expected_session(clock_a),
            _expected_session(clock_b),
        }
        final = _record(directory, watchlist_id)
        assert final["state"] == "SUCCEEDED" and final["session"] == _expected_session(clock_b)
    finally:
        if process_b.poll() is None:
            process_b.kill()
            process_b.wait(10)


def test_pending_api_intent_with_server_down_never_starts_cli(tmp_path):
    """Regression 4: with a pending API intent and serve unreachable the wrapper
    exits busy; the CLI fallback (which cannot replay the idempotency key) is
    never launched."""
    clock = CLOCKS[0]
    directory = tmp_path / "資料 down"
    watchlist_id = _seed(directory, clock)
    session = _expected_session(clock)
    _write_pending(directory, watchlist_id, session, scan_id=None)
    marker = tmp_path / "cli-ran"
    fake = tmp_path / "fake-qscan"
    fake.write_text(f'#!/bin/sh\ntouch \'{marker}\'\necho \'{{"id":"x","state":"SUCCEEDED"}}\'\n')
    fake.chmod(0o755)
    before = (directory / STATE).read_text()
    result = _wrapper(
        [
            "--data-dir",
            str(directory),
            "--watchlist",
            watchlist_id,
            "--port",
            "1",  # nothing listens
            "--qscan",
            str(fake),
        ]
    )
    assert result.returncode == 4, (result.stdout, result.stderr)
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "EXECUTOR_BUSY"
    assert "submitted scan intent" in error["message"]
    assert not marker.exists(), "the CLI fallback must never run for a pending API intent"
    assert (directory / STATE).read_text() == before  # state untouched


def test_force_while_pending_is_refused_not_overwritten(tmp_path):
    """--force on a pending intent is refused (exit 4): the old intent is kept
    intact and no run is created behind its back."""
    clock = CLOCKS[0]
    directory = tmp_path / "資料 force-refused"
    watchlist_id = _seed(directory, clock)
    session = _expected_session(clock)
    key = _write_pending(directory, watchlist_id, session, scan_id=None)
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
        forced = _wrapper(common + ["--force"])
        assert forced.returncode == 4, (forced.stdout, forced.stderr)
        record = _record(directory, watchlist_id)
        assert record["key"] == key and record["state"] == "SUBMITTED"
        token = json.loads((directory / "api-token.json").read_text())["token"]
        assert _runs_for(f"http://127.0.0.1:{port}", token, watchlist_id) == []
        # Resolving the pending intent still works and completes normally.
        resolved = _wrapper(common)
        assert resolved.returncode == 0, (resolved.stdout, resolved.stderr)
        assert len(_runs_for(f"http://127.0.0.1:{port}", token, watchlist_id)) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_concurrent_api_wrappers_single_run(tmp_path):
    """Regression 5: two wrappers racing on the API path neither overwrite the
    state nor create two runs."""
    clock = CLOCKS[0]
    directory = tmp_path / "資料 api-race"
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
            "90",
        ]
        processes = [
            subprocess.Popen(
                [sys.executable, str(WRAPPER_PATH), *common],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONPATH": SRC_ROOT_AND_GUARD},
            )
            for _ in range(2)
        ]
        outputs = [process.communicate(timeout=240) for process in processes]
        assert all(process.returncode in (0, 3, 4) for process in processes), outputs
        token = json.loads((directory / "api-token.json").read_text())["token"]
        assert len(_runs_for(f"http://127.0.0.1:{port}", token, watchlist_id)) == 1
        assert _record(directory, watchlist_id)["state"] == "SUCCEEDED"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_poll_timeout_keeps_pending_then_resumes(tmp_path):
    """A poll timeout must NOT write a terminal FAILED state: the intent stays
    pending and a later wrapper run resumes the same scan to completion."""
    clock = CLOCKS[0]
    directory = tmp_path / "資料 timeout"
    watchlist_id = _seed_list_only(directory, clock)  # no cache: the scan must fetch
    workdir = tmp_path / "harness"
    workdir.mkdir()
    (directory / "hold").write_text("")
    process, port = _start_serve(_span_serve_script(clock), directory, workdir)
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
            "0.5",
            "--timeout-seconds",
            "2",  # expires while the fetch is parked
        ]
        timed_out = _wrapper(common)
        assert timed_out.returncode == 4, (timed_out.stdout, timed_out.stderr)
        record = _record(directory, watchlist_id)
        assert record["state"] == "SUBMITTED", "timeout must keep the intent pending"
        assert record.get("scan_id")
        (directory / "hold").unlink()
        resumed = _wrapper(common + ["--timeout-seconds", "60"])
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert _record(directory, watchlist_id)["state"] == "SUCCEEDED"
        token = json.loads((directory / "api-token.json").read_text())["token"]
        assert len(_runs_for(f"http://127.0.0.1:{port}", token, watchlist_id)) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_mismatched_provider_flag_is_a_configuration_error(tmp_path):
    """The wrapper refuses to submit or dedup when its --provider flag disagrees
    with the provider the server actually runs."""
    clock = CLOCKS[0]
    directory = tmp_path / "資料 provider"
    watchlist_id = _seed(directory, clock)  # seeded and served with fixture
    workdir = tmp_path / "harness"
    workdir.mkdir()
    process, port = _start_serve(_serve_script(clock), directory, workdir)
    try:
        result = _wrapper(
            [
                "--data-dir",
                str(directory),
                "--watchlist",
                watchlist_id,
                "--port",
                str(port),
                "--provider",
                "yahoo",  # wrong: serve runs fixture
            ]
        )
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert json.loads(result.stdout)["error"]["code"] == "USAGE_ERROR"
        # No intent was durably recorded behind the configuration error.
        assert not (directory / STATE).exists() or (
            json.loads((directory / STATE).read_text())["watchlists"] == {}
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_stdout_contract_single_json_no_traceback(tmp_path):
    """Every exit path prints exactly one JSON document on stdout; failures are
    error envelopes without tracebacks or token material."""
    clock = CLOCKS[0]
    directory = tmp_path / "資料 contract"
    watchlist_id = _seed(directory, clock)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    process, port = _start_serve(_serve_script(clock), directory, workdir)
    token = json.loads((directory / "api-token.json").read_text())["token"]
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
    try:
        ok = _wrapper(common)
        assert ok.returncode == 0, (ok.stdout, ok.stderr)
        document = json.loads(ok.stdout)  # exactly one JSON document
        assert document["action"] == "submitted" and document["state"] == "SUCCEEDED"
        assert token not in ok.stderr and token not in ok.stdout

        # Unreadable token file: usage error envelope, no traceback, no token.
        token_file = directory / "api-token.json"
        original = token_file.read_text()
        token_file.write_text("{not json")
        broken = _wrapper(common)
        assert broken.returncode == 2
        assert json.loads(broken.stdout)["error"]["code"] == "USAGE_ERROR"
        assert "daily-scan: USAGE_ERROR" in broken.stderr
        assert "Traceback" not in broken.stderr and token not in broken.stdout
        token_file.write_text(original)
        assert _wrapper(common).returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def _fake_qscan(workdir: Path, scan_stdout: str, scan_exit: int) -> str:
    """Standalone-path stand-in: honest preflight answers, scripted scan output.

    Exercises the wrapper's CLI subprocess contract (exit code AND shape) the
    way a broken or crashed real CLI would hit it.
    """
    script = workdir / "fake-qscan.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            if "sessions" in sys.argv:
                print(json.dumps({"as_of_session": SESSION}))
            elif "watchlist" in sys.argv:
                print(json.dumps([{"id": WL, "name": "daily", "revision": 1}]))
            elif "scans" in sys.argv:
                print(json.dumps([]))
            else:
                sys.stdout.write(SCAN_STDOUT)
                raise SystemExit(SCAN_EXIT)
            """
        )
        .replace("SESSION", repr(_expected_session(CLOCKS[0])))
        .replace("WL", repr(FAKE_WL))
        .replace("SCAN_STDOUT", repr(scan_stdout))
        .replace("SCAN_EXIT", str(scan_exit)),
        encoding="utf-8",
    )
    launcher = workdir / ("fake-qscan.bat" if sys.platform == "win32" else "fake-qscan")
    if sys.platform == "win32":
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n')
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    return str(launcher)


FAKE_WL = "00000000-0000-0000-0000-000000000000"


def test_cli_subprocess_checked_for_exit_code_and_shape(tmp_path):
    """Standalone path: an error envelope is never a run document, non-JSON is
    busy, and a real FAILED run document keeps exit 1."""
    directory = tmp_path / "資料 cli-shape"
    directory.mkdir()
    (directory / "qscan.sqlite3").write_text("")  # initialized-dir marker only
    workdir = tmp_path / "fakes"
    workdir.mkdir()

    def run(fake: str):
        return _wrapper(
            [
                "--data-dir",
                str(directory),
                "--watchlist",
                FAKE_WL,
                "--port",
                "1",
                "--provider",
                "fixture",
                "--qscan",
                fake,
                "--timeout-seconds",
                "30",
            ]
        )

    # qscan scan exits 1 WITH an error envelope: outcome unknown -> busy, and
    # the envelope must not be mistaken for a FAILED run document.
    envelope = run(_fake_qscan(workdir, json.dumps({"error": {"code": "S", "message": "x"}}), 1))
    assert envelope.returncode == 4, (envelope.stdout, envelope.stderr)
    assert json.loads(envelope.stdout)["error"]["code"] == "EXECUTOR_BUSY"

    # qscan scan exits 2 (usage) with an envelope -> configuration error.
    usage = run(_fake_qscan(workdir, json.dumps({"error": {"code": "V", "message": "bad"}}), 2))
    assert usage.returncode == 2

    # qscan scan exits 0 with non-JSON output -> busy, never a fake success.
    garbage = run(_fake_qscan(workdir, "plain text", 0))
    assert garbage.returncode == 4

    # qscan scan exits 0 but prints an envelope: the shape check catches it.
    lying = run(_fake_qscan(workdir, json.dumps({"error": {"code": "X", "message": "y"}}), 0))
    assert lying.returncode == 4

    # A genuine FAILED run document with exit 1 stays exit 1 (no fake success).
    failed = run(
        _fake_qscan(
            workdir,
            json.dumps({"id": "11111111-1111-1111-1111-111111111111", "state": "FAILED"}),
            1,
        )
    )
    assert failed.returncode == 1
    assert json.loads(failed.stdout)["state"] == "FAILED"
