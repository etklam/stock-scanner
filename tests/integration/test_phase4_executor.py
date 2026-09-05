"""Executor ownership, crash recovery with real subprocesses, and migration.

The crash test starts a real server subprocess (uvicorn + ScanExecutor on the same
data directory), holds one job inside the provider via a harness-only file signal,
kills the process, restarts, and verifies recovery against persisted state only.
The control script lives only in the test's tmp directory; production has no such
switch and no HTTP route can influence the worker.
"""

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from datetime import UTC, date, datetime
from pathlib import Path

from filelock import FileLock
from sqlalchemy import text

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import RawPrices
from qscan.bootstrap import bootstrap
from qscan.domain.models import RunState

SESSIONS = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
CLOSES = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
KEY_FIRST = "crash-key-first-0000001"
KEY_SECOND = "crash-key-second-0000002"

CONTROL_SCRIPT = textwrap.dedent(
    """
    import json, sys, time
    from datetime import UTC, date, datetime
    from pathlib import Path

    data_dir = Path(sys.argv[1])
    hold = Path(sys.argv[2])
    port_file = Path(sys.argv[3])
    port = int(sys.argv[4])

    from qscan.adapters.calendar import FixedClock, NYSECalendar
    from qscan.adapters.providers import FixtureProvider
    from qscan.application.contracts import RawPrices
    from qscan.bootstrap import bootstrap
    from qscan.executor import NullLock, ScanExecutor
    from qscan.interfaces.api.app import create_app

    class HoldableProvider(FixtureProvider):
        # Test harness only: fetch parks while the hold file exists.
        def fetch(self, instrument, start, end):
            deadline = time.monotonic() + 120
            while hold.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            return super().fetch(instrument, start, end)

    calendar = NYSECalendar()
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    closes = CLOSES
    provider = HoldableProvider({"GOOD": RawPrices(tuple(zip(sessions, closes, strict=True)))})
    scanner = bootstrap(
        provider, data_dir=data_dir, initialize=False, service_lock=NullLock(),
        clock=clock, calendar=calendar,
    )
    from filelock import FileLock
    ownership = FileLock(scanner.data_dir / "executor.lock", timeout=5)
    ownership.acquire(timeout=5)
    executor = ScanExecutor(scanner, poll_seconds=0.05, stop_grace=10)
    executor.start()
    app = create_app(scanner, token_path=data_dir / "api-token.json", executor=executor)
    import uvicorn
    with port_file.open("w") as stream:
        stream.write(str(port))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    """
).replace("CLOSES", repr(CLOSES))

SRC_ROOT = str(Path(__file__).parents[2] / "src")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(data_dir: Path, hold: Path, workdir: Path) -> tuple[subprocess.Popen, int]:
    port = free_port()
    port_file = workdir / f"port-{port}.txt"
    script = workdir / "server_control.py"
    if not script.exists():
        script.write_text(CONTROL_SCRIPT, encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script), str(data_dir), str(hold), str(port_file), str(port)],
        cwd=workdir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": SRC_ROOT},
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if port_file.exists():
            wait_http_ready(port)
            return process, port
        if process.poll() is not None:
            raise AssertionError("Server subprocess exited during startup")
        time.sleep(0.05)
    raise AssertionError("Server subprocess did not report its port in time")


def wait_http_ready(port: int, timeout: float = 60) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=2)
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise AssertionError("HTTP server never became reachable")


def seed_storage(tmp_path: Path) -> Path:
    """Create schema, token, and one completed run as Phase 3.5 would have left it."""
    directory = tmp_path / "資料 crash"
    calendar = NYSECalendar()
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    provider = FixtureProvider({"GOOD": RawPrices(tuple(zip(SESSIONS, CLOSES, strict=True)))})
    app = bootstrap(provider, data_dir=directory, clock=clock, calendar=calendar)
    try:
        watchlist = app.watchlists.create("wl", ["GOOD"])
        app.scans.scan(watchlist.id, as_of=date(2026, 9, 4))
    finally:
        app.close()
    from qscan.interfaces.api.localauth import ensure_token

    ensure_token(directory / "api-token.json")
    return directory


def read_run(directory: Path, identity: str) -> dict | None:
    from qscan.adapters.persistence.repository import open_database

    engine = open_database(directory / "qscan.sqlite3", readonly=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT state, document FROM scan_runs WHERE id = :i"), {"i": identity}
            ).first()
            return {"state": row[0], "document": json.loads(row[1])} if row else None
    finally:
        engine.dispose()


def wait_for_state(
    port: int, token: str, scan_id: str, states: set[str], timeout: float = 90
) -> dict:
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/scans/{scan_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if response.status_code == 200 and response.json()["state"] in states:
            return response.json()
        time.sleep(0.1)
    raise AssertionError(f"Run {scan_id} never reached {states}")


def submit(port: int, token: str, watchlist: str, key: str) -> str:
    import httpx

    response = httpx.post(
        f"http://127.0.0.1:{port}/api/v1/scans",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        json={"watchlist_id": watchlist, "as_of_session": "2026-09-04", "data_mode": "force"},
        timeout=10,
    )
    assert response.status_code in (200, 202), response.text
    return response.json()["id"]


def watchlist_id_of(directory: Path) -> str:
    from qscan.adapters.persistence.repository import open_database

    engine = open_database(directory / "qscan.sqlite3", readonly=True)
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT id FROM watchlists LIMIT 1")).scalar_one()
    finally:
        engine.dispose()


def test_kill_running_server_recovers_on_restart(tmp_path):
    directory = seed_storage(tmp_path)
    workdir = tmp_path / "控制 harness"
    workdir.mkdir()
    hold = tmp_path / "hold-fetch"
    process, port = start_server(directory, hold, workdir)
    token = json.loads((directory / "api-token.json").read_text())["token"]
    watchlist = watchlist_id_of(directory)
    history = submit(port, token, watchlist, "crash-key-history-00001")
    history_before = wait_for_state(port, token, history, {"SUCCEEDED"})
    # Hold the provider: the first job claims and parks inside fetch, the second
    # stays QUEUED. Then hard-kill the whole server process.
    hold.touch()
    first = submit(port, token, watchlist, KEY_FIRST)
    second = submit(port, token, watchlist, KEY_SECOND)
    running = wait_for_state(port, token, first, {"RUNNING"})
    assert running["started_at"] is not None
    assert running["counts"] is None  # no fake data_error counts while running
    queued = wait_for_state(port, token, second, {"QUEUED"})
    assert queued["state"] == "QUEUED"
    first_before_kill = read_run(directory, first)
    assert first_before_kill is not None and first_before_kill["state"] == "RUNNING"
    process.kill()
    process.wait(30)
    hold.unlink()  # second life runs to completion

    process2, port2 = start_server(directory, hold, workdir)
    try:
        # Startup recovery: the orphaned RUNNING run becomes FAILED/WORKER_INTERRUPTED
        # keeping its identity, original started_at and no published results.
        wait_for_state(port2, token, first, {"FAILED"})
        first_after = read_run(directory, first)
        assert first_after is not None
        assert first_after["document"]["error"] == "WORKER_INTERRUPTED"
        assert first_after["document"]["started_at"] == first_before_kill["document"]["started_at"]
        assert first_after["document"]["input_hash"] is None
        # A terminal run looks terminal: no live progress, counts closed out.
        assert first_after["document"]["progress"] is None
        assert first_after["document"]["counts"] == {
            "requested": 1,
            "evaluated": 0,
            "excluded": 0,
            "data_error": 1,
            "candidate": 0,
        }
        # The QUEUED job resumes and completes under the same id.
        restarted = wait_for_state(port2, token, second, {"SUCCEEDED"})
        assert restarted["counts"]["candidate"] == 1
        # The pre-crash completed run is untouched and readable.
        history_after = read_run(directory, history)
        assert history_after is not None
        assert history_after["state"] == "SUCCEEDED"
        assert history_after["document"]["counts"] == history_before["counts"]
        # The same idempotency key still maps to the original interrupted run.
        assert submit(port2, token, watchlist, KEY_FIRST) == first
    finally:
        process2.kill()
        process2.wait(30)


def run_cli_scan(directory: Path, workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from qscan.interfaces.cli import app; app()",
            "--data-dir",
            str(directory),
            "--provider",
            "fixture",
            "scan",
            "--watchlist",
            "wl",
            "--as-of",
            "2026-09-04",
            "--data-mode",
            "cache_only",
            "--format",
            "json",
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": SRC_ROOT},
    )


def test_second_serve_and_cli_scan_collide_with_owner(tmp_path):
    directory = seed_storage(tmp_path)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    owner = FileLock(directory / "executor.lock", timeout=1)
    owner.acquire(timeout=1)
    try:
        # Direct CLI scan while another process owns the directory: exit 4, no run row.
        result = run_cli_scan(directory, workdir)
        assert result.returncode == 4, (result.returncode, result.stdout, result.stderr)
        assert json.loads(result.stdout)["error"]["code"] == "EXECUTOR_LOCKED"
        assert read_run(directory, "00000000-0000-0000-0000-000000000000") is None
    finally:
        owner.release()
    # After release, the same CLI scan succeeds synchronously (CLI stays standalone).
    result = run_cli_scan(directory, workdir)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["state"] == "SUCCEEDED"


def test_migration_0002_to_0003_preserves_history(tmp_path):
    """Upgrade a 0002 database holding a legacy run document; history stays readable."""
    from alembic import command
    from alembic.config import Config

    from qscan.adapters.persistence.repository import open_database

    directory = tmp_path / "資料 migration"
    calendar = NYSECalendar()
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    provider = FixtureProvider({"GOOD": RawPrices(tuple(zip(SESSIONS, CLOSES, strict=True)))})
    app = bootstrap(provider, data_dir=directory, clock=clock, calendar=calendar)
    try:
        watchlist = app.watchlists.create("wl", ["GOOD"])
        run = app.scans.scan(watchlist.id, as_of=date(2026, 9, 4))
    finally:
        app.close()

    legacy = tmp_path / "資料 migration-legacy"
    legacy.mkdir()
    engine = open_database(legacy / "qscan.sqlite3")
    config = Config()
    config.set_main_option("path_separator", "os")
    config.set_main_option(
        "script_location",
        str(Path(SRC_ROOT) / "qscan" / "adapters" / "persistence" / "migrations"),
    )
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0002")
    legacy_document = {
        key: value
        for key, value in json.loads(run.model_dump_json(exclude={"results"})).items()
        if key not in {"progress", "data_mode", "warnings"}
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO scan_runs (id, owner_id, state, created_at, source_run_id,"
                " input_hash, document) VALUES (:i, 'local', :s, :c, NULL, :h, :d)"
            ),
            {
                "i": str(run.id),
                "s": RunState.SUCCEEDED.value,
                "c": run.requested_at.isoformat(),
                "h": run.input_hash,
                "d": json.dumps(legacy_document),
            },
        )
        source = open_database(directory / "qscan.sqlite3", readonly=True)
        with source.connect() as src:
            instrument_rows = [
                {**m} for m in src.execute(text("SELECT * FROM instruments")).mappings()
            ]
            watchlist_rows = [
                {**m} for m in src.execute(text("SELECT * FROM watchlists")).mappings()
            ]
            member_rows = [
                {**m} for m in src.execute(text("SELECT * FROM watchlist_members")).mappings()
            ]
            result_rows = [
                {**m}
                for m in src.execute(
                    text(
                        "SELECT run_id, instrument_id, is_candidate, score, document"
                        " FROM scan_results WHERE run_id = :i"
                    ),
                    {"i": str(run.id)},
                ).mappings()
            ]
        source.dispose()
        for row in instrument_rows:
            connection.execute(
                text(
                    "INSERT INTO instruments (id, provider_symbol, market, document, cache_info)"
                    " VALUES (:id, :provider_symbol, :market, :document, :cache_info)"
                ),
                row,
            )
        for row in watchlist_rows:
            connection.execute(
                text(
                    "INSERT INTO watchlists (id, owner_id, name, revision, created_at, updated_at)"
                    " VALUES (:id, :owner_id, :name, :revision, :created_at, :updated_at)"
                ),
                row,
            )
        for row in member_rows:
            connection.execute(
                text(
                    "INSERT INTO watchlist_members (watchlist_id, instrument_id, position,"
                    " document) VALUES (:watchlist_id, :instrument_id, :position, :document)"
                ),
                row,
            )
        for row in result_rows:
            connection.execute(
                text(
                    "INSERT INTO scan_results (run_id, instrument_id, is_candidate, score,"
                    " document) VALUES (:run_id, :instrument_id, :is_candidate, :score,"
                    " :document)"
                ),
                row,
            )
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0002')"))
    engine.dispose()

    import shutil

    shutil.copytree(directory / "snapshots", legacy / "snapshots")
    # Documented upgrade path: run init (backup first), which migrates 0002 -> head.
    upgraded = bootstrap(provider, data_dir=legacy, initialize=True, clock=clock, calendar=calendar)
    upgraded.close()
    reopened = bootstrap(
        provider, data_dir=legacy, initialize=False, clock=clock, calendar=calendar
    )
    try:
        loaded = reopened.queries.get(run.id)
        assert loaded.state == RunState.SUCCEEDED
        assert loaded.results == run.results
        assert loaded.input_hash == run.input_hash
        page, _ = reopened.queries.page(limit=10)
        assert page[0].id == run.id
        # The queue machinery works on the migrated database.
        queued = reopened.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
        created, _ = reopened.repository.enqueue(queued, "legacy-key-00000001", "hash", 20)
        assert created
        assert reopened.repository.next_queued() == queued.id
        claimed = queued.model_copy(update={"state": RunState.RUNNING, "started_at": clock.now()})
        assert reopened.repository.claim(claimed)
    finally:
        reopened.close()


def test_progress_never_overrides_terminal_and_readers_dont_block(tmp_path):
    """Reader GETs and submissions complete while a job is stuck mid-scan."""
    from fastapi.testclient import TestClient

    from qscan.executor import ScanExecutor
    from qscan.interfaces.api.app import create_app

    directory = tmp_path / "資料 busy"
    calendar = NYSECalendar()
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    provider = FixtureProvider({"GOOD": RawPrices(tuple(zip(SESSIONS, CLOSES, strict=True)))})
    app = bootstrap(provider, data_dir=directory, clock=clock, calendar=calendar)
    import threading

    release = threading.Event()
    original = provider.fetch

    def holding(instrument, start, end):
        release.wait(30)
        return original(instrument, start, end)

    provider.fetch = holding
    from qscan.interfaces.api.localauth import ensure_token

    ensure_token(tmp_path / "t.json")
    token = json.loads((tmp_path / "t.json").read_text())["token"]
    executor = ScanExecutor(app, poll_seconds=0.05, stop_grace=5)
    fastapi = create_app(
        app, token_path=tmp_path / "t.json", executor=executor, allowed_hosts=("testserver",)
    )
    executor.start()
    try:
        with TestClient(fastapi) as client:
            headers = {"Authorization": f"Bearer {token}"}
            watchlist = client.post(
                "/api/v1/watchlists", headers=headers, json={"name": "wl", "symbols": ["GOOD"]}
            ).json()["id"]
            body = {"watchlist_id": watchlist, "as_of_session": "2026-09-04", "data_mode": "force"}
            scan_id = client.post(
                "/api/v1/scans",
                headers=headers | {"Idempotency-Key": "busy-key-0000000001"},
                json=body,
            ).json()["id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = client.get(f"/api/v1/scans/{scan_id}", headers=headers).json()
                if status["state"] == "RUNNING":
                    break
                time.sleep(0.02)
            assert status["state"] == "RUNNING"
            # All of these are served by the request threadpool while the worker
            # thread is stuck inside the provider.
            assert client.get("/health/ready").status_code == 200
            assert client.get(f"/api/v1/scans/{scan_id}", headers=headers).status_code == 200
            assert client.get("/api/v1/watchlists", headers=headers).status_code == 200
            created = client.post(
                "/api/v1/scans",
                headers=headers | {"Idempotency-Key": "busy-key-0000000002"},
                json=body,
            )
            assert created.status_code == 202
            assert client.get("/api/v1/scans", headers=headers).json()["next_cursor"] is None
    finally:
        release.set()
        executor.stop()
        app.close()
