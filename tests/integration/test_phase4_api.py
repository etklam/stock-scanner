"""Phase 4 API behaviour: idempotency, queue, pagination, security, owner scope.

All tests are offline; the fixture provider records every fetch call so tests can
assert that GET routes never trigger provider I/O.
"""

import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import ApplicationContext, RawPrices
from qscan.bootstrap import bootstrap
from qscan.executor import ScanExecutor
from qscan.interfaces.api.app import MAX_BODY_BYTES, create_app
from qscan.interfaces.api.localauth import ensure_token, rotate_token

SESSIONS = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
RISING_CLOSED = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
RISING = tuple(zip(SESSIONS, RISING_CLOSED, strict=True))
FLAT = tuple(zip(SESSIONS, (100.0,) * len(SESSIONS), strict=True))
FALLING = tuple(zip(SESSIONS, (200.0 - i * 0.2 for i in range(len(SESSIONS))), strict=True))
KEY_A = "idempotency-key-a-000001"
KEY_B = "idempotency-key-b-000002"


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        clock: FixedClock | None = None,
        data_dir_override: Path | None = None,
        **kwargs,
    ) -> None:
        self.calendar = NYSECalendar()
        self.clock = clock or FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
        self.data_dir = data_dir_override or (tmp_path / "資料 data")
        self.provider = FixtureProvider(
            {"GOOD": RawPrices(RISING), "FLAT": RawPrices(FLAT), "DOWN": RawPrices(FALLING)}
        )
        self.principal = kwargs.pop("principal", "local")
        self.app = bootstrap(
            self.provider,
            data_dir=self.data_dir,
            clock=self.clock,
            calendar=self.calendar,
            context=ApplicationContext(self.principal),
            **kwargs,
        )
        token_path = tmp_path / f"api-token-{self.principal}.json"
        ensure_token(token_path)
        if self.principal != "local":
            # Harness-only: a second local server would carry its own principal file.
            document = json.loads(token_path.read_text())
            document["principal"] = self.principal
            token_path.write_text(json.dumps(document))
        self.executor = ScanExecutor(self.app, poll_seconds=0.05, stop_grace=10.0)
        fastapi = create_app(
            self.app,
            token_path=token_path,
            executor=self.executor,
            queue_limit=kwargs.get("queue_limit", 20),
            allowed_hosts=("testserver",),
        )
        self.token = json.loads(token_path.read_text())["token"]
        self.executor.start()
        self.client = TestClient(fastapi)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def close(self) -> None:
        self.executor.stop()
        self.app.close()

    def make_watchlist(self, symbols=("GOOD",), name="wl") -> str:
        response = self.client.post(
            "/api/v1/watchlists",
            headers=self.headers,
            json={"name": name, "symbols": list(symbols)},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def submit(self, watchlist_id: str, key: str = KEY_A, body: dict | None = None) -> "object":
        payload = {
            "watchlist_id": watchlist_id,
            "as_of_session": "2026-09-04",
            "data_mode": "force",
            **(body or {}),
        }
        return self.client.post(
            "/api/v1/scans", headers=self.headers | {"Idempotency-Key": key}, json=payload
        )

    def wait_terminal(self, scan_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            document = self.client.get(f"/api/v1/scans/{scan_id}", headers=self.headers).json()
            if document["state"] in ("SUCCEEDED", "PARTIAL", "FAILED"):
                return document
            time.sleep(0.05)
        raise AssertionError("Scan did not reach a terminal state in time")


@pytest.fixture
def harness(tmp_path):
    value = Harness(tmp_path)
    yield value
    value.close()


def test_submit_persists_immediately_and_worker_reuses_same_id(harness):
    watchlist = harness.make_watchlist()
    before = len(harness.provider.calls)
    response = harness.submit(watchlist)
    assert response.status_code == 202
    assert response.headers["location"] == f"/api/v1/scans/{response.json()['id']}"
    scan_id = response.json()["id"]
    persisted = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.headers).json()
    assert persisted["state"] in ("QUEUED", "RUNNING", "SUCCEEDED")
    terminal = harness.wait_terminal(scan_id)
    assert terminal["state"] == "SUCCEEDED"
    assert terminal["counts"] == {
        "requested": 1,
        "evaluated": 1,
        "excluded": 0,
        "data_error": 0,
        "candidate": 1,
    }
    assert terminal["finished_at"] is not None and terminal["started_at"] is not None
    detail = harness.client.get(f"/api/v1/scans/{scan_id}/results", headers=harness.headers).json()[
        "items"
    ][0]
    assert detail["rank"] == 1 and detail["analysis"]["is_candidate"] is True
    # One symbol fetch: submission did not fetch; the single worker execution did.
    assert len(harness.provider.calls) - before == 1


def test_queued_run_has_null_counts_and_started_at(harness):
    blocker = threading.Event()
    original = harness.provider.fetch

    def slow(instrument, start, end):
        blocker.wait(5)
        return original(instrument, start, end)

    harness.provider.fetch = slow
    watchlist = harness.make_watchlist()
    scan_id = harness.submit(watchlist).json()["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.headers).json()
        if status["state"] == "RUNNING":
            break
        time.sleep(0.02)
    assert status["state"] == "RUNNING"
    assert status["started_at"] is not None and status["counts"] is None
    assert status["progress"]["phase"] == "market_data"
    assert status["progress"]["processed_symbols"] == 0
    blocker.set()
    terminal = harness.wait_terminal(scan_id)
    assert terminal["state"] == "SUCCEEDED" and terminal["progress"] is None


def test_idempotency_retry_after_date_roll_and_watchlist_edit(harness):
    watchlist = harness.make_watchlist()
    scan_id = harness.submit(watchlist).json()["id"]
    harness.wait_terminal(scan_id)
    # The market date rolls and the list changes; a retry with the same key and
    # request shape must return the originally accepted job, not a new run.
    harness.clock.instant += timedelta(days=3)
    harness.client.put(
        f"/api/v1/watchlists/{watchlist}/symbols",
        headers=harness.headers,
        json={"expected_revision": 1, "symbols": ["GOOD", "FLAT"]},
    )
    retry = harness.submit(watchlist)
    assert retry.status_code == 200
    assert retry.json()["id"] == scan_id
    assert retry.json()["state"] == "SUCCEEDED"
    runs = harness.client.get("/api/v1/scans", headers=harness.headers).json()["items"]
    assert len(runs) == 1
    # Deleted watchlist: the accepted job is still returned.
    scan_id2 = harness.submit(watchlist, key=KEY_B).json()["id"]
    harness.wait_terminal(scan_id2)
    harness.clock.instant += timedelta(days=3)
    harness.client.delete(f"/api/v1/watchlists/{watchlist}", headers=harness.headers)
    retry2 = harness.submit(watchlist, key=KEY_B)
    assert retry2.status_code == 200 and retry2.json()["id"] == scan_id2


def test_idempotency_conflict_and_validation_before_key(harness):
    watchlist = harness.make_watchlist()
    assert harness.submit(watchlist).status_code == 202
    different = harness.submit(watchlist, body={"data_mode": "cache_only"})
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert harness.submit(watchlist).status_code == 200
    missing = harness.client.post(
        "/api/v1/scans", headers=harness.headers, json={"watchlist_id": watchlist}
    )
    assert missing.status_code == 422
    unknown = harness.submit(str(uuid4()), key=f"unknown-key-{uuid4().hex[:10]}")
    assert unknown.status_code == 404


def test_concurrent_same_key_creates_one_run(harness):
    watchlist = harness.make_watchlist(("GOOD", "FLAT"))
    results: list = []

    def fire() -> None:
        results.append(harness.submit(watchlist))

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    created = [r for r in results if r.status_code == 202]
    replayed = [r for r in results if r.status_code == 200]
    assert len(created) == 1
    assert len(created) + len(replayed) == 8
    identifiers = {r.json()["id"] for r in results}
    assert len(identifiers) == 1
    assert len(harness.client.get("/api/v1/scans", headers=harness.headers).json()["items"]) == 1


def test_queue_limit_full_and_existing_key_still_readable(tmp_path):
    harness = Harness(tmp_path)
    blocker = threading.Event()
    original = harness.provider.fetch

    def slow(instrument, start, end):
        blocker.wait(10)
        return original(instrument, start, end)

    harness.provider.fetch = slow
    try:
        with TestClient(harness.client.app) as client:
            harness.client = client
            watchlist = harness.make_watchlist()
            first = harness.submit(watchlist, key=KEY_A)
            assert first.status_code == 202  # occupies the worker
            for key in (f"filler-key-{i:06d}" for i in range(20)):
                response = harness.submit(watchlist, key=key)
                assert response.status_code == 202
            overflow = harness.submit(watchlist, key="overflow-key-00000001")
            assert overflow.status_code == 429
            assert overflow.json()["error"]["code"] == "QUEUE_LIMIT_REACHED"
            # The legal retry of an accepted job still returns even when full.
            replay = harness.submit(watchlist, key=KEY_A)
            assert replay.status_code == 200
            assert replay.json()["id"] == first.json()["id"]
    finally:
        blocker.set()
        harness.close()


def test_api_cli_same_snapshot_same_results(harness):
    watchlist = harness.make_watchlist()
    scan_id = harness.submit(watchlist).json()["id"]
    http_terminal = harness.wait_terminal(scan_id)
    cli_run = harness.app.queries.get(scan_id)
    assert cli_run.state.value == http_terminal["state"]
    assert cli_run.counts.model_dump() == http_terminal["counts"]
    http_result = harness.client.get(
        f"/api/v1/scans/{scan_id}/results", headers=harness.headers
    ).json()["items"][0]
    cli_result = cli_run.results[0]
    assert http_result["analysis"]["score"] == cli_result.analysis.score
    http_stage = http_result["analysis"]["selected_window"]["stage"]
    assert http_stage == cli_result.analysis.selected_window.stage
    assert http_result["rank"] == cli_result.rank
    # CLI synchronous scan of the same inputs yields identical score/stage/rank.
    cli_scan = harness.app.scans.scan(watchlist_id=watchlist, as_of=date(2026, 9, 4))
    assert cli_scan.results[0].analysis.score == http_result["analysis"]["score"]
    assert cli_scan.results[0].rank == http_result["rank"]


def test_state_semantics_zero_partial_failed_not_ready(harness):
    # DOWN evaluates cleanly with zero candidates; valid data, no setup, still success.
    zero = harness.make_watchlist(("DOWN",), "zero")
    zero_id = harness.submit(zero).json()["id"]
    assert harness.wait_terminal(zero_id)["state"] == "SUCCEEDED"
    # BTC-USD resolves UNSUPPORTED (excluded); FLAT is quarantined; MISSING has no data.
    harness.provider.data["MISSING"] = None
    partial = harness.make_watchlist(("GOOD", "FLAT", "MISSING", "BTC-USD"), "partial")
    partial_id = harness.submit(partial, key=KEY_B).json()["id"]
    assert harness.wait_terminal(partial_id)["state"] == "PARTIAL"
    failed = harness.make_watchlist(("MISSING",), "failed")
    failed_id = harness.submit(failed, key="failed-key-000000001").json()["id"]
    failed_terminal = harness.wait_terminal(failed_id)
    assert failed_terminal["state"] == "FAILED"
    assert failed_terminal["counts"]["data_error"] == 1
    not_ready_list = harness.make_watchlist(("GOOD",), "notready")
    blocker = threading.Event()
    original = harness.provider.fetch

    def slow(instrument, start, end):
        blocker.wait(5)
        return original(instrument, start, end)

    harness.provider.fetch = slow
    busy_id = harness.submit(not_ready_list, key="busy-key-0000000001").json()["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = harness.client.get(f"/api/v1/scans/{busy_id}", headers=harness.headers).json()
        if status["state"] == "RUNNING":
            break
        time.sleep(0.02)
    for suffix in ("results", "changes"):
        response = harness.client.get(f"/api/v1/scans/{busy_id}/{suffix}", headers=harness.headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SCAN_NOT_READY"
    failed_results = harness.client.get(
        f"/api/v1/scans/{failed_id}/results", headers=harness.headers
    )
    assert failed_results.status_code == 409
    assert failed_results.json()["error"]["code"] == "SCAN_FAILED"
    failed_status = harness.client.get(f"/api/v1/scans/{failed_id}", headers=harness.headers)
    assert failed_status.status_code == 200  # diagnostics stay readable
    blocker.set()
    harness.wait_terminal(busy_id)


def test_results_cursor_pagination_ties_nulls_and_filters(harness):
    # DOWN evaluates but never becomes a candidate (score null); GOOD ties nobody.
    watchlist = harness.make_watchlist(("GOOD", "FLAT", "DOWN"))
    scan_id = harness.submit(watchlist).json()["id"]
    harness.wait_terminal(scan_id)
    seen: list = []
    cursor = None
    while True:
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        page = harness.client.get(
            f"/api/v1/scans/{scan_id}/results", headers=harness.headers, params=params
        ).json()
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 3  # no duplicates or gaps across pages
    scores = [item["analysis"]["score"] for item in seen]
    ordered = sorted(scores, key=lambda s: (s is None, -(s or 0)))
    assert scores == ordered
    candidates = harness.client.get(
        f"/api/v1/scans/{scan_id}/results",
        headers=harness.headers,
        params={"candidate": "true"},
    ).json()["items"]
    assert len(candidates) == 1
    stage_page = harness.client.get(
        f"/api/v1/scans/{scan_id}/results",
        headers=harness.headers,
        params={"stage": "NEAR_CLOSE_RESISTANCE", "limit": 200},
    ).json()
    assert all(item["analysis"]["stage"] == "NEAR_CLOSE_RESISTANCE" for item in stage_page["items"])
    malformed = harness.client.get(
        f"/api/v1/scans/{scan_id}/results", headers=harness.headers, params={"cursor": "garbage"}
    )
    assert malformed.status_code == 400
    # A cursor minted for one run must never paginate another run
    # (cross-run rejection is pinned in test_phase41.py).


def test_scans_cursor_pagination_order_and_filters(harness):
    watchlist = harness.make_watchlist()
    for index in range(5):
        scan_id = harness.submit(watchlist, key=f"page-key-{index:08d}").json()["id"]
        harness.wait_terminal(scan_id)
    first = harness.client.get("/api/v1/scans", headers=harness.headers, params={"limit": 2}).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = harness.client.get(
        "/api/v1/scans",
        headers=harness.headers,
        params={"limit": 200, "cursor": first["next_cursor"]},
    ).json()
    identifiers = {item["id"] for item in first["items"]} | {item["id"] for item in second["items"]}
    assert len(identifiers) == 5
    created = [item["requested_at"] + item["id"] for item in first["items"] + second["items"]]
    assert created == sorted(created, reverse=True)
    succeeded = harness.client.get(
        "/api/v1/scans", headers=harness.headers, params={"state": "SUCCEEDED"}
    ).json()
    assert len(succeeded["items"]) == 5
    bad_state = harness.client.get(
        "/api/v1/scans", headers=harness.headers, params={"state": "EXPLODED"}
    )
    assert bad_state.status_code == 400


def test_series_changes_export_read_snapshot_without_provider(tmp_path):
    harness = Harness(tmp_path)
    try:
        with TestClient(harness.client.app) as client:
            harness.client = client
            watchlist = harness.make_watchlist()
            scan_id = harness.submit(watchlist).json()["id"]
            harness.wait_terminal(scan_id)
            results = client.get(
                f"/api/v1/scans/{scan_id}/results", headers=harness.headers
            ).json()["items"]
            instrument = results[0]["instrument"]["id"]
            calls_before = len(harness.provider.calls)
            network_guard = threading.Event()

            def forbidden(*args):
                network_guard.set()
                raise AssertionError("Provider I/O during GET")

            harness.provider.fetch = forbidden
            series = client.get(
                f"/api/v1/scans/{scan_id}/series/{instrument}", headers=harness.headers
            ).json()
            assert series["displayed_sessions"] == 126
            assert len(series["closes"]) == 126 and "sma" in series
            limited = client.get(
                f"/api/v1/scans/{scan_id}/series/{instrument}",
                headers=harness.headers,
                params={"limit": 10},
            ).json()
            assert limited["displayed_sessions"] == 10
            changes = client.get(f"/api/v1/scans/{scan_id}/changes", headers=harness.headers).json()
            assert changes["binding"] == "recorded" and changes["previous_run_id"] is None
            export = client.get(f"/api/v1/scans/{scan_id}/export", headers=harness.headers)
            assert export.status_code == 200
            assert export.headers["x-scan-state"] == "SUCCEEDED"
            assert int(export.headers["x-result-count"]) == 1
            # CSV keeps the CLI UTF-8 BOM convention.
            assert export.text.lstrip("\ufeff").splitlines()[0].startswith("scan_id,")
            assert not network_guard.is_set()
            assert len(harness.provider.calls) == calls_before
    finally:
        harness.close()


def test_watchlist_crud_revision_conflict_and_in_use(tmp_path):
    harness = Harness(tmp_path)
    blocker = threading.Event()
    original = harness.provider.fetch

    def slow(instrument, start, end):
        blocker.wait(10)
        return original(instrument, start, end)

    harness.provider.fetch = slow
    try:
        with TestClient(harness.client.app) as client:
            harness.client = client
            watchlist = harness.make_watchlist(("GOOD", "FLAT"))
            renamed = client.patch(
                f"/api/v1/watchlists/{watchlist}",
                headers=harness.headers,
                json={"name": "renamed", "expected_revision": 1},
            )
            assert renamed.status_code == 200 and renamed.json()["revision"] == 2
            conflict = client.patch(
                f"/api/v1/watchlists/{watchlist}",
                headers=harness.headers,
                json={"name": "again", "expected_revision": 1},
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "WATCHLIST_VERSION_CONFLICT"
            symbols = client.put(
                f"/api/v1/watchlists/{watchlist}/symbols",
                headers=harness.headers,
                json={"expected_revision": 2, "symbols": ["DOWN"]},
            )
            assert symbols.status_code == 200 and symbols.json()["symbols"] == ["DOWN"]
            # One job occupies the worker; the second stays QUEUED while blocked.
            busy = harness.make_watchlist(("GOOD",), "busy")
            client.post(
                "/api/v1/scans",
                headers=harness.headers | {"Idempotency-Key": KEY_A},
                json={"watchlist_id": busy, "as_of_session": "2026-09-04"},
            )
            client.post(
                "/api/v1/scans",
                headers=harness.headers | {"Idempotency-Key": KEY_B},
                json={"watchlist_id": busy, "as_of_session": "2026-09-04"},
            )
            in_use = client.delete(f"/api/v1/watchlists/{busy}", headers=harness.headers)
            assert in_use.status_code == 409
            assert in_use.json()["error"]["code"] == "WATCHLIST_IN_USE"
            # Unblock; once nothing is queued/running, deletion succeeds and the
            # completed runs stay readable.
            blocker.set()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                states = {
                    item["state"]
                    for item in client.get("/api/v1/scans", headers=harness.headers).json()["items"]
                }
                if not states & {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.05)
            assert not states & {"QUEUED", "RUNNING"}
            deleted = client.delete(f"/api/v1/watchlists/{busy}", headers=harness.headers)
            assert deleted.status_code == 204
            runs = client.get("/api/v1/scans", headers=harness.headers).json()["items"]
            assert runs and all(item["state"] == "SUCCEEDED" for item in runs)
            missing_after = client.get(f"/api/v1/watchlists/{busy}", headers=harness.headers)
            assert missing_after.status_code == 404
    finally:
        blocker.set()
        harness.close()


def test_owner_scope_cross_principal(harness, tmp_path):
    watchlist = harness.make_watchlist()
    scan_id = harness.submit(watchlist).json()["id"]
    harness.wait_terminal(scan_id)
    other = Harness(tmp_path, principal="second", data_dir_override=harness.data_dir)
    try:
        with TestClient(other.client.app) as client:
            for path in (
                f"/api/v1/watchlists/{watchlist}",
                f"/api/v1/scans/{scan_id}",
                f"/api/v1/scans/{scan_id}/results",
                f"/api/v1/scans/{scan_id}/changes",
            ):
                response = client.get(path, headers=other.headers)
                assert response.status_code == 404, (path, response.status_code)
            response = client.get(f"/api/v1/scans/{scan_id}/results", headers=other.headers)
            assert response.status_code == 404
            own_scan = client.post(
                "/api/v1/scans",
                headers=other.headers | {"Idempotency-Key": "cross-owner-key-00001"},
                json={"watchlist_id": watchlist, "as_of_session": "2026-09-04"},
            )
            assert own_scan.status_code == 404
            # Other principal's idempotency key namespace is independent.
            assert other.app.repository.run_by_idempotency(KEY_A) is None
    finally:
        other.close()


def test_auth_host_origin_and_errors(harness):
    assert harness.client.get("/health/live").status_code == 200
    assert harness.client.get("/health/ready").status_code == 200
    assert b"/" not in harness.client.get("/health/live").content
    for headers, expected in (
        ({}, 401),
        ({"Authorization": "Basic abc"}, 401),
        ({"Authorization": f"Bearer {harness.token}x"}, 401),
    ):
        response = harness.client.get("/api/v1/watchlists", headers=headers)
        assert response.status_code == expected
    wrong_host = harness.client.get("/health/live", headers={"Host": "evil.example:8000"})
    assert wrong_host.status_code == 403
    assert wrong_host.json()["error"]["code"] == "FORBIDDEN"
    bad_origin = harness.client.get("/health/live", headers={"Origin": "http://evil.example"})
    assert bad_origin.status_code == 403
    null_origin = harness.client.get(
        "/api/v1/watchlists",
        headers=harness.headers | {"Origin": "null"},
    )
    assert null_origin.status_code == 403
    preflight = harness.client.options(
        "/api/v1/watchlists",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Host": "testserver",
        },
    )
    assert preflight.status_code == 403
    assert "access-control-allow-origin" not in preflight.headers
    method = harness.client.delete("/api/v1/scans", headers=harness.headers)
    assert method.status_code == 405
    assert method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    invalid_body = harness.client.post(
        "/api/v1/watchlists", headers=harness.headers, content=b"{not json"
    )
    assert invalid_body.status_code == 422
    unknown_field = harness.client.post(
        "/api/v1/watchlists",
        headers=harness.headers,
        json={"name": "x", "symbols": ["GOOD"], "symbols_extra": 1},
    )
    assert unknown_field.status_code == 422
    assert unknown_field.json()["error"]["code"] == "VALIDATION_ERROR"
    non_uuid = harness.client.get("/api/v1/watchlists/nope", headers=harness.headers)
    assert non_uuid.status_code == 422  # malformed path parameter
    envelope = harness.client.get(
        "/api/v1/scans/00000000-0000-0000-0000-000000000000", headers=harness.headers
    ).json()
    assert set(envelope["error"]) == {"code", "message", "details", "request_id"}


def test_body_limit_enforced(tmp_path):
    harness = Harness(tmp_path)
    try:
        with TestClient(harness.client.app) as client:
            symbols = ["GOOD"] * 2000
            response = client.post(
                "/api/v1/watchlists",
                headers={**harness.headers, "content-length": str(MAX_BODY_BYTES + 1)},
                json={"name": "big", "symbols": symbols},
            )
            assert response.status_code == 413
            oversized = client.post(
                "/api/v1/watchlists",
                headers=harness.headers,
                json={"name": "x" * 200, "symbols": ["GOOD"]},
            )
            assert oversized.status_code == 422
            too_many = client.post(
                "/api/v1/watchlists",
                headers=harness.headers,
                json={"name": "y", "symbols": ["GOOD"] * 2001},
            )
            assert too_many.status_code == 422
    finally:
        harness.close()


def test_token_rotation_keeps_principal_and_invalidates_old(tmp_path):
    harness = Harness(tmp_path)
    try:
        with TestClient(harness.client.app) as client:
            old_token = harness.token
            assert client.get("/api/v1/watchlists", headers=harness.headers).status_code == 200
            rotated = rotate_token(tmp_path / "api-token-local.json")
            assert rotated != old_token
            stale = {"Authorization": f"Bearer {old_token}"}
            fresh = {"Authorization": f"Bearer {rotated}"}
            assert client.get("/api/v1/watchlists", headers=stale).status_code == 401
            assert client.get("/api/v1/watchlists", headers=fresh).status_code == 200
            # Token string never becomes the principal: same owner, same data.
            wl = client.post(
                "/api/v1/watchlists",
                headers=fresh,
                json={"name": "after-rotation", "symbols": ["GOOD"]},
            )
            assert wl.status_code == 201
            assert len(client.get("/api/v1/watchlists", headers=fresh).json()) == 1
    finally:
        harness.close()


def test_get_routes_never_create_work_or_fetch(harness):
    watchlist = harness.make_watchlist()
    scan_id = harness.submit(watchlist).json()["id"]
    harness.wait_terminal(scan_id)
    before = len(harness.provider.calls)
    runs_before = len(harness.client.get("/api/v1/scans", headers=harness.headers).json()["items"])
    for _ in range(3):
        harness.client.get("/api/v1/scans", headers=harness.headers)
        harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.headers)
        harness.client.get("/api/v1/watchlists", headers=harness.headers)
    assert len(harness.provider.calls) == before
    runs_after = harness.client.get("/api/v1/scans", headers=harness.headers).json()["items"]
    assert len(runs_after) == runs_before
