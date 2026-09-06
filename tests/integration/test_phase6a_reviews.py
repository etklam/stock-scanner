"""Phase 6A backend: review labels, Origin allowlist, and the local UI mount.

Reviews are minimal mutable human data scoped to (owner, run, instrument):
they never touch scores/ranks/hashes, never carry across runs or replays, and
conflicts are refused instead of silently overwritten. The Origin allowlist is
formed from trusted server config only; the compiled UI shell is the single
anonymous static mount and /api 404s are never swallowed by an SPA fallback.
"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_phase4_api import Harness

from qscan.adapters.backup import create_backup, restore_backup
from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.persistence.repository import migrate, open_database
from qscan.adapters.providers import FixtureProvider
from qscan.bootstrap import bootstrap
from qscan.executor import ScanExecutor
from qscan.interfaces.api.app import create_app
from qscan.interfaces.api.localauth import ensure_token

SRC_ROOT = str(Path(__file__).parents[2] / "src")


@pytest.fixture
def reviewed(tmp_path):
    """A SUCCEEDED single-symbol run plus its harness."""
    harness = Harness(tmp_path)
    try:
        watchlist = harness.make_watchlist(("GOOD",))
        accepted = harness.submit(watchlist)
        assert accepted.status_code == 202, accepted.text
        scan_id = accepted.json()["id"]
        document = harness.wait_terminal(scan_id)
        assert document["state"] == "SUCCEEDED", document
        results = harness.client.get(
            f"/api/v1/scans/{scan_id}/results", headers=harness.headers
        ).json()["items"]
        yield harness, scan_id, results[0]["instrument"]["id"]
    finally:
        harness.close()


def test_review_save_list_and_revision_conflict(reviewed):
    harness, scan_id, instrument = reviewed
    base = f"/api/v1/scans/{scan_id}/reviews"
    saved = harness.client.put(
        f"{base}/{instrument}",
        headers=harness.headers,
        json={"label": "worth_reviewing", "note": "tight base on rising closes"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    # A second blind save is refused: it would silently overwrite.
    blind = harness.client.put(
        f"{base}/{instrument}", headers=harness.headers, json={"label": "not_useful"}
    )
    assert blind.status_code == 409
    assert blind.json()["error"]["code"] == "REVIEW_REVISION_CONFLICT"
    # Wrong expected revision also refused.
    stale = harness.client.put(
        f"{base}/{instrument}",
        headers=harness.headers,
        json={"label": "borderline", "expected_revision": 99},
    )
    assert stale.status_code == 409
    # Correct revision updates and bumps.
    updated = harness.client.put(
        f"{base}/{instrument}",
        headers=harness.headers,
        json={"label": "borderline", "note": "still watching", "expected_revision": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2 and updated.json()["label"] == "borderline"
    # The batched list shows exactly one saved label (one row request, not N).
    listing = harness.client.get(base, headers=harness.headers)
    assert listing.status_code == 200
    assert [item["instrument_id"] for item in listing.json()["items"]] == [instrument]
    assert listing.json()["items"][0]["note"] == "still watching"


def test_review_only_evaluated_and_unknown_instruments(reviewed):
    harness, scan_id, _ = reviewed
    mixed = harness.make_watchlist(("GOOD", "FLAT"), name="mixedwl")
    accepted = harness.submit(mixed, key="review-flat-00000001")
    flat_scan = accepted.json()["id"]
    assert harness.wait_terminal(flat_scan)["state"] == "PARTIAL"
    flat_items = harness.client.get(
        f"/api/v1/scans/{flat_scan}/results?candidate=false", headers=harness.headers
    ).json()["items"]
    # FLAT is a data error (SUSPICIOUS_FLAT_SERIES), i.e. NOT evaluated.
    assert flat_items and flat_items[0]["category"] == "data_error"
    # An exclusion is not a verdict: labeling one is refused.
    refused = harness.client.put(
        f"/api/v1/scans/{flat_scan}/reviews/{flat_items[0]['instrument']['id']}",
        headers=harness.headers,
        json={"label": "worth_reviewing"},
    )
    assert refused.status_code == 400
    # Unknown instrument under a finished run -> 404.
    missing = harness.client.put(
        f"/api/v1/scans/{scan_id}/reviews/00000000-0000-0000-0000-000000000000",
        headers=harness.headers,
        json={"label": "borderline"},
    )
    assert missing.status_code == 404
    # Over-long note is a validation error (envelope, not HTML).
    long_note = harness.client.put(
        f"/api/v1/scans/{scan_id}/reviews/{flat_items[0]['instrument']['id']}",
        headers=harness.headers,
        json={"label": "borderline", "note": "x" * 501},
    )
    assert long_note.status_code == 422


def test_reviews_need_published_results(reviewed):
    harness, _, instrument = reviewed
    watchlist = harness.make_watchlist(("GOOD",), name="pending")
    accepted = harness.submit(watchlist, key="review-pending-000001")
    pending_id = accepted.json()["id"]

    def reached_terminal():
        document = harness.client.get(f"/api/v1/scans/{pending_id}", headers=harness.headers).json()
        return document["state"] in ("SUCCEEDED", "PARTIAL", "FAILED")

    deadline = time.monotonic() + 10
    while not reached_terminal() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert reached_terminal()
    listing = harness.client.get(f"/api/v1/scans/{pending_id}/reviews", headers=harness.headers)
    assert listing.status_code == 200  # listing a finished run is fine (empty)
    assert listing.json()["items"] == []


def test_replay_starts_unlabeled_and_results_unchanged(reviewed):
    """The review store never leaks into immutable scan results or replays."""
    harness, scan_id, instrument = reviewed
    before = harness.client.get(
        f"/api/v1/scans/{scan_id}/results/{instrument}", headers=harness.headers
    ).json()
    assert (
        harness.client.put(
            f"/api/v1/scans/{scan_id}/reviews/{instrument}",
            headers=harness.headers,
            json={"label": "worth_reviewing", "note": "keep"},
        ).status_code
        == 200
    )
    after = harness.client.get(
        f"/api/v1/scans/{scan_id}/results/{instrument}", headers=harness.headers
    ).json()
    assert before == after, "saving a review must not touch the scan result"
    replayed = harness.app.scans.replay(scan_id)
    assert replayed.results == harness.app.repository.run(scan_id).results
    assert harness.app.reviews.list(replayed.id) == ()


def test_cross_principal_reviews_are_invisible_and_unwritable(reviewed):
    harness, _, instrument = reviewed
    scan_id = harness.client.get("/api/v1/scans", headers=harness.headers).json()["items"][0]["id"]
    assert (
        harness.client.put(
            f"/api/v1/scans/{scan_id}/reviews/{instrument}",
            headers=harness.headers,
            json={"label": "worth_reviewing"},
        ).status_code
        == 200
    )
    other = harness.app.for_principal("someone-else")
    # Invisible: even listing the run's reviews is a NOT_FOUND, never [].
    with pytest.raises(Exception) as excinfo:
        other.reviews.list(scan_id)
    assert excinfo.value.code.value == "NOT_FOUND"
    with pytest.raises(Exception) as excinfo:
        other.reviews.save(scan_id, instrument, "worth_reviewing", "", None)
    assert excinfo.value.code.value == "NOT_FOUND"  # unwritable, existence hidden


def test_review_backup_restore_roundtrip(reviewed, tmp_path):
    harness, scan_id, instrument = reviewed
    assert (
        harness.client.put(
            f"/api/v1/scans/{scan_id}/reviews/{instrument}",
            headers=harness.headers,
            json={"label": "borderline", "note": "backup me"},
        ).status_code
        == 200
    )
    archive = tmp_path / "backups" / "with-reviews.zip"
    archive.parent.mkdir()
    create_backup(harness.data_dir, archive)
    destination = tmp_path / "資料 restored"
    restore_backup(archive, destination)
    restored = bootstrap(
        FixtureProvider({}),
        data_dir=destination,
        initialize=False,
        clock=FixedClock(harness.clock.now()),
        calendar=NYSECalendar(),
    )
    try:
        labels = restored.reviews.list(scan_id)
        assert len(labels) == 1
        assert labels[0].label == "borderline" and labels[0].note == "backup me"
    finally:
        restored.close()


def test_review_export_includes_saved_labels(tmp_path, reviewed):
    harness, scan_id, _ = reviewed
    instrument = harness.client.get(
        f"/api/v1/scans/{scan_id}/results", headers=harness.headers
    ).json()["items"][0]["instrument"]["id"]
    harness.client.put(
        f"/api/v1/scans/{scan_id}/reviews/{instrument}",
        headers=harness.headers,
        json={"label": "worth_reviewing", "note": "export me"},
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from qscan.interfaces.cli import app; app()",
            "--data-dir",
            str(harness.data_dir),
            "--provider",
            "fixture",
            "scans",
            "review-export",
            str(scan_id),
            "--output",
            str(tmp_path / "review.csv"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": SRC_ROOT},
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    summary = json.loads(completed.stdout)
    assert summary["labeled_count"] == 1 and summary["exported_at"]
    assert "not part of the immutable scan snapshot" in summary["note"]
    sheet = (tmp_path / "review.csv").read_text(encoding="utf-8-sig")
    assert "export me" in sheet and "worth_reviewing" in sheet
    assert "review_revision" in sheet and "review_updated_at" in sheet


def test_same_origin_browser_post_passes_external_and_null_refused(tmp_path):
    """Same-origin POST must work for the local UI; foreign/null Origins stay
    refused. The allowlist comes from trusted server configuration only."""
    harness = Harness(tmp_path, allowed_origins=("http://127.0.0.1:8000", "http://localhost:8000"))
    try:
        watchlist = harness.make_watchlist()
        same_origin = harness.client.post(
            "/api/v1/scans",
            headers=harness.headers
            | {
                "Idempotency-Key": "origin-same-00000001",
                "Origin": "http://127.0.0.1:8000",
            },
            json={"watchlist_id": watchlist, "as_of_session": "2026-09-04"},
        )
        assert same_origin.status_code == 202, same_origin.text
        for origin in ("https://evil.example", "null"):
            refused = harness.client.post(
                "/api/v1/watchlists",
                headers=harness.headers | {"Origin": origin},
                json={"name": "x", "symbols": ["GOOD"]},
            )
            assert refused.status_code == 403, origin
            assert refused.json()["error"]["code"] == "FORBIDDEN"
        # No Origin (native client) keeps working as before.
        assert harness.client.get("/api/v1/watchlists", headers=harness.headers).status_code == 200
    finally:
        harness.close()


def _with_ui(tmp_path, harness, dist: Path | None):
    token_path = tmp_path / f"ui-token-{dist is not None}.json"
    ensure_token(token_path)
    executor = ScanExecutor(harness.app, poll_seconds=0.05, stop_grace=5.0)
    fastapi = create_app(
        harness.app,
        token_path=token_path,
        executor=executor,
        allowed_hosts=("testserver", "127.0.0.1", "localhost"),
        allowed_origins=("http://127.0.0.1:8765", "http://localhost:8765"),
        ui_assets=dist,
    )
    executor.start()
    return executor, TestClient(fastapi)


def test_ui_mount_serves_shell_anonymous_and_api_404_stays_json(tmp_path):
    harness = Harness(tmp_path)
    try:
        dist = tmp_path / "ui-dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text(
            "<!doctype html><html><body>qscan ui shell</body></html>", encoding="utf-8"
        )
        (dist / "assets" / "app.js").write_text("console.log('ui')", encoding="utf-8")
        executor, client = _with_ui(tmp_path, harness, dist)
        try:
            shell = client.get("/ui/")
            assert shell.status_code == 200 and "ui shell" in shell.text  # anonymous
            assert "authorization" not in {k.lower() for k in shell.request.headers}
            assert client.get("/ui/assets/app.js").status_code == 200
            # Unknown UI asset: StaticFiles 404, never an SPA-faked 200 page.
            assert client.get("/ui/assets/nope.js").status_code == 404
            # Business API 404 is the JSON envelope, not an SPA HTML page.
            # Without a token an unknown path is 401 (routes are not revealed);
            # with the token it is the plain 404 envelope.
            assert client.get("/api/v1/definitely-not-a-route").status_code == 401
            ui_token = json.loads((tmp_path / "ui-token-True.json").read_text())["token"]
            missing = client.get(
                "/api/v1/definitely-not-a-route",
                headers={"Authorization": f"Bearer {ui_token}"},
            )
            assert missing.status_code == 404
            assert missing.headers["content-type"].startswith("application/json")
            assert missing.json()["error"]["code"] == "NOT_FOUND"
            # /api still requires the bearer token even with a UI mounted.
            assert client.get("/api/v1/watchlists").status_code == 401
        finally:
            executor.stop()
    finally:
        harness.close()


def test_ui_missing_assets_show_build_hint(tmp_path):
    harness = Harness(tmp_path)
    try:
        executor, client = _with_ui(tmp_path, harness, tmp_path / "no-such-dist")
        try:
            response = client.get("/ui/")
            assert response.status_code == 404
            assert "npm" in response.json()["error"]["message"]
            assert client.get("/ui/assets/app.js").status_code == 404
        finally:
            executor.stop()
    finally:
        harness.close()


def test_old_schema_upgrade_adds_reviews_table(tmp_path):
    """Migration 0004 is additive: a 0003-era directory upgrades through init."""
    directory = tmp_path / "資料 legacy"
    app = bootstrap(
        FixtureProvider({}),
        data_dir=directory,
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=NYSECalendar(),
    )
    try:
        with app.engine.begin() as connection:
            # Roll the directory back to a pre-0004 state.
            connection.exec_driver_sql("DROP TABLE scan_reviews")
            connection.exec_driver_sql("DELETE FROM alembic_version")
            connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0003')")
    finally:
        app.close()
    engine = open_database(directory / "qscan.sqlite3")
    try:
        migrate(engine)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
            connection.exec_driver_sql("SELECT label, note, revision FROM scan_reviews")
    finally:
        engine.dispose()
