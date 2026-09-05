"""Phase 4.1 regressions: reliability and API-contract fixes.

Each test pins behaviour the previous suite asserted too weakly or not at all:
shutdown ownership, worker error boundaries, terminal failure invariants,
queued-execution identity, cursor binding, and the OpenAPI contract. The
subprocess test drives the production run_serve entry point, not a harness
re-implementation of it.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock, Timeout
from sqlalchemy import text

import qscan.application.reporting as reporting
from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import (
    ApplicationContext,
    ApplicationError,
    Counts,
    Progress,
    RawPrices,
)
from qscan.bootstrap import bootstrap
from qscan.domain.models import ErrorCode, RunState
from qscan.executor import ScanExecutor
from qscan.interfaces.api.app import create_app
from qscan.interfaces.api.localauth import ensure_token

SESSIONS = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
CLOSES = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
RISING = RawPrices(tuple(zip(SESSIONS, CLOSES, strict=True)))
FLAT = RawPrices(tuple(zip(SESSIONS, (100.0,) * len(SESSIONS), strict=True)))
TERMINAL = {RunState.SUCCEEDED, RunState.PARTIAL, RunState.FAILED}
SRC_ROOT = str(Path(__file__).parents[2] / "src")

SERVE_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from datetime import date
    from pathlib import Path

    data_dir = Path(sys.argv[1])
    hold = Path(sys.argv[2])
    port = int(sys.argv[3])

    from qscan.adapters.calendar import NYSECalendar
    from qscan.adapters.providers import FixtureProvider
    from qscan.application.contracts import RawPrices
    from qscan.interfaces.api.app import run_serve

    class HoldableProvider(FixtureProvider):
        # Harness only: fetch parks while the hold file exists.
        def fetch(self, instrument, start, end):
            deadline = time.monotonic() + 120
            while hold.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            return super().fetch(instrument, start, end)

    sessions = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    provider = HoldableProvider(
        {"GOOD": RawPrices(tuple(zip(sessions, CLOSES, strict=True)))}
    )
    raise SystemExit(
        run_serve(
            provider,
            data_dir=data_dir,
            token_path=data_dir / "api-token.json",
            port=port,
            stop_grace=2,
            log_level="error",
        )
    )
    """
).replace("CLOSES", repr(CLOSES))


def make_app(tmp_path: Path, data: dict | None = None):
    provider = FixtureProvider(data if data is not None else {"GOOD": RISING, "FLAT": FLAT})
    app = bootstrap(
        provider,
        data_dir=tmp_path / "資料 data",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=NYSECalendar(),
        context=ApplicationContext("local"),
    )
    executor = ScanExecutor(app, poll_seconds=0.05, stop_grace=5)
    return app, provider, executor


def wait_run(app, identity: UUID, states: set[RunState], timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = app.repository.run_summary(identity)
        if run.state in states:
            return run
        time.sleep(0.02)
    raise AssertionError(f"Run {identity} never reached {states}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _graceful_stop_serve(process: subprocess.Popen) -> None:
    """Trigger the same graceful stop a console Ctrl+C would, per OS.

    Windows cannot deliver SIGINT to another process (Popen raises
    "Unsupported signal: 2"); the real console Ctrl+C a serve user types
    arrives as a console control event instead. The child was created in its
    own process group so the event targets the server; the test ignores
    SIGINT for the moment in case the console broadcasts it.
    """
    if sys.platform == "win32":
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            process.send_signal(signal.CTRL_C_EVENT)
        finally:
            signal.signal(signal.SIGINT, previous)
    else:
        process.send_signal(signal.SIGINT)


def _redacted_serve_logs(workdir: Path, token: str, limit: int = 4000) -> str:
    """Redacted tail of the serve subprocess output for failure diagnostics."""
    pieces = []
    for name in ("serve-stdout.log", "serve-stderr.log"):
        path = workdir / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if token:
            text = text.replace(token, "<redacted-token>")
        pieces.append(f"--- {name} (tail) ---\n{text[-limit:]}")
    return "\n".join(pieces)


def test_shutdown_timeout_exits_process_and_releases_ownership(tmp_path):
    """A worker parked past the stop grace must not outlive the ownership lock.

    run_serve exits the process instead of returning, so worker and lock die
    together and the RUNNING scan is recovered by the next startup.
    """
    directory = tmp_path / "資料 serve"
    seed = bootstrap(
        FixtureProvider({"GOOD": RISING}),
        data_dir=directory,
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=NYSECalendar(),
    )
    try:
        seed.watchlists.create("wl", ["GOOD"])
    finally:
        seed.close()

    workdir = tmp_path / "harness"
    workdir.mkdir()
    script = workdir / "serve_control.py"
    script.write_text(SERVE_SCRIPT, encoding="utf-8")
    hold = tmp_path / "hold-fetch"
    hold.touch()
    port = free_port()
    # Child output goes to files (not DEVNULL) so failures carry diagnostics;
    # handles are closed in the parent right after spawn.
    output_handles = [
        (workdir / "serve-stdout.log").open("wb"),
        (workdir / "serve-stderr.log").open("wb"),
    ]
    try:
        process = subprocess.Popen(
            [sys.executable, str(script), str(directory), str(hold), str(port)],
            cwd=workdir,
            stdout=output_handles[0],
            stderr=output_handles[1],
            env={**os.environ, "PYTHONPATH": SRC_ROOT},
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
        )
    finally:
        for handle in output_handles:
            handle.close()
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
            raise AssertionError(
                "serve subprocess never became reachable\n" + _redacted_serve_logs(workdir, "")
            )
        token = json.loads((directory / "api-token.json").read_text())["token"]
        headers = {"Authorization": f"Bearer {token}"}
        watchlist_id = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/watchlists", headers=headers, timeout=5
        ).json()[0]["id"]
        scan_id = httpx.post(
            f"http://127.0.0.1:{port}/api/v1/scans",
            headers=headers | {"Idempotency-Key": "shutdown-key-000001"},
            json={
                "watchlist_id": watchlist_id,
                "as_of_session": "2026-09-04",
                "data_mode": "force",
            },
            timeout=10,
        ).json()["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = httpx.get(
                f"http://127.0.0.1:{port}/api/v1/scans/{scan_id}", headers=headers, timeout=5
            ).json()["state"]
            if state == "RUNNING":
                break
            time.sleep(0.05)
        assert state == "RUNNING"

        # Graceful stop with the worker parked mid-scan: the process itself must
        # exit (worker and lock die together), not return while the worker lives.
        _graceful_stop_serve(process)
        try:
            returncode = process.wait(30)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                "serve subprocess survived the graceful stop beyond 30s; the worker "
                f"outlived the stop grace\n{_redacted_serve_logs(workdir, token)}"
            ) from exc
        assert returncode == 1, (
            f"serve exited {returncode} instead of the stop-timeout exit code 1\n"
            + _redacted_serve_logs(workdir, token)
        )

        # The ownership lock died with the process: acquirable immediately.
        lock = FileLock(directory / "executor.lock", timeout=1)
        try:
            lock.acquire(timeout=1)
        except Timeout as exc:
            raise AssertionError(
                "executor lock still held after serve exited; ownership outlived "
                f"the worker\n{_redacted_serve_logs(workdir, token)}"
            ) from exc
        lock.release()

        # The interrupted scan is finalized by the next startup's recovery.
        app = bootstrap(
            FixtureProvider({}),
            data_dir=directory,
            clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
            calendar=NYSECalendar(),
        )
        try:
            assert app.repository.recover_interrupted() == 1
            run = app.repository.run_summary(UUID(scan_id))
            assert run.state is RunState.FAILED
            assert run.error is ErrorCode.WORKER_INTERRUPTED
            assert run.progress is None
            assert run.counts == Counts(requested=1, data_error=1)
        finally:
            app.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_pre_claim_failure_backs_off_then_fails_poison_job(tmp_path):
    """A job that fails before its claim retries with backoff, then leaves the
    queue as FAILED instead of hot-looping or blocking the jobs behind it."""
    app, _provider, executor = make_app(tmp_path)
    try:
        watchlist = app.watchlists.create("wl", ["GOOD"])
        run = app.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
        app.repository.create_run(run)

        class ExplodingCalendar:
            def __init__(self, inner):
                self._inner, self.calls = inner, 0

            @property
            def version(self):
                return self._inner.version

            def resolve(self, requested, now):
                return self._inner.resolve(requested, now)

            def sessions(self, start, end):
                self.calls += 1
                raise RuntimeError("calendar unavailable")

        exploding = ExplodingCalendar(app.calendar)
        app.scans.calendar = exploding
        executor.start()
        try:
            failed = wait_run(app, run.id, {RunState.FAILED})
            assert failed.error is ErrorCode.SCAN_FAILED
            assert failed.counts == Counts(requested=1)  # nothing was ever executed
            assert any("gave up" in warning for warning in failed.warnings)
            assert exploding.calls >= 5  # retried with backoff before giving up
            assert executor.alive
            # The queue is not stuck: a healthy job behind the poison completes.
            app.scans.calendar = app.calendar
            healthy = app.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
            app.repository.create_run(healthy)
            assert wait_run(app, healthy.id, TERMINAL).state is RunState.SUCCEEDED
        finally:
            executor.stop()
    finally:
        app.close()


def test_post_claim_bind_failure_marks_run_failed(tmp_path, monkeypatch):
    """An error after the claim (baseline binding) becomes a terminal FAILED,
    not a run stuck RUNNING until the next startup."""
    app, _provider, executor = make_app(tmp_path)

    def explode(self, run):
        raise RuntimeError("baseline binding unavailable")

    monkeypatch.setattr(reporting.ComparisonService, "bind", explode)
    try:
        watchlist = app.watchlists.create("wl", ["GOOD"])
        run = app.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
        app.repository.create_run(run)
        executor.start()
        try:
            failed = wait_run(app, run.id, {RunState.FAILED})
            assert failed.error is ErrorCode.SCAN_FAILED
            assert failed.started_at is not None  # it really was claimed first
            assert failed.counts == Counts(requested=1, data_error=1)
            assert executor.alive  # the worker survives and keeps serving
        finally:
            executor.stop()
    finally:
        app.close()


def test_recovery_closes_counts_and_progress(tmp_path):
    """A recovered run is terminal-shaped: no live progress, counts closed out."""
    app, _provider, _executor = make_app(tmp_path)
    try:
        watchlist = app.watchlists.create("wl", ["GOOD"])
        run = app.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
        now = app.clock.now().astimezone(UTC)
        claimed = run.model_copy(
            update={
                "state": RunState.RUNNING,
                "started_at": now,
                "progress": Progress(
                    phase="market_data", processed_symbols=0, total_symbols=1, updated_at=now
                ),
            }
        )
        app.repository.create_run(claimed)
        assert app.repository.recover_interrupted() == 1
        recovered = app.repository.run_summary(run.id)
        assert recovered.state is RunState.FAILED
        assert recovered.error is ErrorCode.WORKER_INTERRUPTED
        assert recovered.progress is None
        assert recovered.counts == Counts(requested=1, data_error=1)
        assert recovered.finished_at is not None
    finally:
        app.close()


def test_recovery_aborts_on_unreadable_document(tmp_path):
    """Recovery never plants an unparseable document; it aborts and rolls back."""
    app, _provider, _executor = make_app(tmp_path)
    try:
        bogus = "00000000-0000-0000-0000-0000000000ef"
        with app.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO scan_runs (id, owner_id, state, created_at, document)"
                    " VALUES (:i, 'local', 'RUNNING', '2026-09-05T12:00:00+00:00', :d)"
                ),
                {"i": bogus, "d": json.dumps({"state": "RUNNING"})},
            )
        with pytest.raises(ApplicationError):
            app.repository.recover_interrupted()
        with app.engine.connect() as connection:
            state = connection.execute(
                text("SELECT state FROM scan_runs WHERE id = :i"), {"i": bogus}
            ).scalar_one()
        assert state == "RUNNING"  # rolled back, left for inspection or restore
    finally:
        app.close()


def test_incompatible_queued_run_fails_without_executing(tmp_path):
    """A queued job recorded under another engine or provider is failed as
    EXECUTION_INCOMPATIBLE, never silently executed with new semantics."""
    app, provider, executor = make_app(tmp_path)
    try:
        watchlist = app.watchlists.create("wl", ["GOOD"])
        old_engine = app.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
        old_engine = old_engine.model_copy(
            update={"context": old_engine.context.model_copy(update={"engine_version": "0.0.0"})}
        )
        app.repository.create_run(old_engine)
        other_provider = app.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
        other_provider = other_provider.model_copy(update={"provider": "someone-else"})
        app.repository.create_run(other_provider)
        executor.start()
        try:
            engine_failed = wait_run(app, old_engine.id, {RunState.FAILED})
            provider_failed = wait_run(app, other_provider.id, {RunState.FAILED})
            for failed in (engine_failed, provider_failed):
                assert failed.error is ErrorCode.EXECUTION_INCOMPATIBLE
                assert failed.input_hash is None  # never executed
                assert any("accepted by engine" in warning for warning in failed.warnings)
            assert provider.calls == []  # the provider was never asked for data
        finally:
            executor.stop()
    finally:
        app.close()


def test_zero_evaluation_failure_reports_error_and_reasons(tmp_path):
    """A FAILED run with zero evaluations carries a non-null error and a reason
    breakdown, because results endpoints refuse FAILED runs."""
    app, _provider, executor = make_app(
        tmp_path, data={"NOPE": ApplicationError(ErrorCode.UNSUPPORTED_INSTRUMENT)}
    )
    try:
        watchlist = app.watchlists.create("wl", ["NOPE"])
        run = app.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
        app.repository.create_run(run)
        executor.start()
        try:
            failed = wait_run(app, run.id, {RunState.FAILED})
            assert failed.error is ErrorCode.SCAN_FAILED
            assert any("no candidates" in warning for warning in failed.warnings)
            assert "UNSUPPORTED_INSTRUMENT" in " ".join(failed.warnings)
            assert failed.counts == Counts(requested=1, excluded=1)
        finally:
            executor.stop()
    finally:
        app.close()


def test_results_cursor_is_bound_to_its_run(tmp_path):
    """A cursor minted for run A is rejected on run B instead of silently
    paginating someone else's results."""
    app, _provider, executor = make_app(tmp_path)
    try:
        ensure_token(tmp_path / "t.json")
        token = json.loads((tmp_path / "t.json").read_text())["token"]
        headers = {"Authorization": f"Bearer {token}"}
        fastapi = create_app(
            app, token_path=tmp_path / "t.json", executor=executor, allowed_hosts=("testserver",)
        )
        executor.start()
        client = TestClient(fastapi)
        wide = client.post(
            "/api/v1/watchlists",
            headers=headers,
            json={"name": "wide", "symbols": ["GOOD", "FLAT"]},
        ).json()["id"]
        narrow = client.post(
            "/api/v1/watchlists",
            headers=headers,
            json={"name": "narrow", "symbols": ["GOOD"]},
        ).json()["id"]

        def submit(watchlist_id: str, key: str) -> str:
            response = client.post(
                "/api/v1/scans",
                headers=headers | {"Idempotency-Key": key},
                json={
                    "watchlist_id": watchlist_id,
                    "as_of_session": "2026-09-04",
                    "data_mode": "force",
                },
            )
            assert response.status_code == 202, response.text
            return response.json()["id"]

        wide_id, narrow_id = (
            submit(wide, "cursor-key-wide-000001"),
            submit(narrow, "cursor-key-narrow-0001"),
        )
        wait_run(app, UUID(wide_id), TERMINAL)
        wait_run(app, UUID(narrow_id), TERMINAL)

        first = client.get(
            f"/api/v1/scans/{wide_id}/results", headers=headers, params={"limit": 1}
        ).json()
        assert first["next_cursor"], "expected a paginated run for this test"
        cross = client.get(
            f"/api/v1/scans/{narrow_id}/results",
            headers=headers,
            params={"cursor": first["next_cursor"], "limit": 1},
        )
        assert cross.status_code == 400
        assert cross.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        executor.stop()
        app.close()


def test_streamed_body_over_limit_is_rejected_413(tmp_path):
    """The actual byte-counting branch (no Content-Length, chunked body) answers
    exactly 413 PAYLOAD_TOO_LARGE, driven at the ASGI boundary."""
    app, _provider, _executor = make_app(tmp_path)
    try:
        ensure_token(tmp_path / "t.json")
        token = json.loads((tmp_path / "t.json").read_text())["token"]
        fastapi = create_app(app, token_path=tmp_path / "t.json", allowed_hosts=("testserver",))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/watchlists",
            "raw_path": b"/api/v1/watchlists",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"authorization", f"Bearer {token}".encode()),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
        }
        chunks = [b"x" * 600_000, b"x" * 600_000]  # 1.2 MB, no Content-Length declared
        sent: list[dict] = []

        async def receive():
            if chunks:
                return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        asyncio.run(fastapi(scope, receive, send))
    finally:
        app.close()

    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert start["status"] == 413
    assert b"PAYLOAD_TOO_LARGE" in body


def test_openapi_contract_matches_wiring(tmp_path):
    """The documented contract names the models actually returned: ScanAccepted,
    the idempotent 200 replay, Comparison, text/csv, and the ErrorEnvelope 422."""
    app, _provider, _executor = make_app(tmp_path)
    try:
        ensure_token(tmp_path / "t.json")
        fastapi = create_app(app, token_path=tmp_path / "t.json", allowed_hosts=("testserver",))
        schema = fastapi.openapi()
    finally:
        app.close()

    def ref_of(response: dict) -> str:
        return response["content"]["application/json"]["schema"]["$ref"]

    submit = schema["paths"]["/api/v1/scans"]["post"]["responses"]
    assert ref_of(submit["202"]).endswith("/ScanAccepted")
    assert ref_of(submit["200"]).endswith("/ScanStatusOut")
    changes = schema["paths"]["/api/v1/scans/{identity}/changes"]["get"]["responses"]
    assert ref_of(changes["200"]).endswith("/Comparison")
    export = schema["paths"]["/api/v1/scans/{identity}/export"]["get"]["responses"]["200"]
    assert list(export["content"]) == ["text/csv"]
    components = schema["components"]["schemas"]
    assert "ErrorEnvelope" in components
    assert "HTTPValidationError" not in components

    def assert_no_default_validation(node):
        if isinstance(node, dict):
            assert not node.get("$ref", "").endswith("/HTTPValidationError")
            for value in node.values():
                assert_no_default_validation(value)
        elif isinstance(node, list):
            for value in node:
                assert_no_default_validation(value)

    assert_no_default_validation(schema)
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            invalid = operation.get("responses", {}).get("422")
            if invalid is not None:
                assert ref_of(invalid).endswith("/ErrorEnvelope")
