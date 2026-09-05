"""Deterministic regressions for Phase 3.5, including DBAPI read boundaries."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event
from test_phase3 import pair
from test_phase3 import setup as phase_setup

from qscan.application.contracts import Counts
from qscan.domain.models import RunState


@pytest.fixture
def setup(tmp_path):
    yield from phase_setup.__wrapped__(tmp_path)


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("resource", ["run", "watchlist", "cache"])
def test_multiselect_read_is_one_sqlite_snapshot(setup, resource, readonly):
    app, _, _, _ = setup
    watchlist, _, completed = pair(setup)
    identity = uuid4()
    running = completed.model_copy(
        update={
            "id": identity,
            "state": RunState.RUNNING,
            "results": (),
            "counts": Counts(requested=1, data_error=1),
            "finished_at": None,
        }
    )
    app.repository.create_run(running)
    old_cache = app.repository.cache(watchlist.instruments[0])
    start_write, committed = Event(), Event()
    observed = []
    second_select = {
        "run": "FROM scan_results",
        "watchlist": "FROM watchlist_members",
        "cache": "FROM prices_by_provider",
    }[resource]

    def writer():
        assert start_write.wait(10), "Reader did not reach second SELECT"
        if resource == "run":
            app.repository.publish(completed.model_copy(update={"id": identity}))
        elif resource == "watchlist":
            app.watchlists.import_content(
                "test", b"NEW", watchlist_id=watchlist.id, expected_revision=1
            )
        else:
            app.repository.replace_cache(
                watchlist.instruments[0],
                old_cache.model_copy(
                    update={
                        "series": old_cache.series.model_copy(
                            update={"closes": tuple(c * 2 for c in old_cache.series.closes)}
                        )
                    }
                ),
            )
        committed.set()

    def interleave(connection, cursor, statement, parameters, context, executemany):
        if second_select in statement and not start_write.is_set():
            observed.append(connection.connection.driver_connection.in_transaction)
            start_write.set()
            assert committed.wait(10), "Writer could not commit while reader held its snapshot"

    from qscan.bootstrap import bootstrap

    reader = (
        bootstrap(setup[1], data_dir=app.data_dir, initialize=False, readonly=True)
        if readonly
        else app
    )
    event.listen(reader.engine, "before_cursor_execute", interleave)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            if resource == "run":
                value = reader.repository.run(identity)
                consistent = (
                    value.state == RunState.RUNNING
                    and value.results == ()
                    and value.counts == running.counts
                )
            elif resource == "watchlist":
                value = reader.repository.watchlist(watchlist.id)
                consistent = value == watchlist
            else:
                value = reader.repository.cache(watchlist.instruments[0])
                consistent = value == old_cache
            future.result(timeout=10)
        assert consistent, f"Mixed {resource} state; DBAPI in_transaction={observed}"
        assert observed == [True], "SELECTs require an actual SQLite BEGIN"
    finally:
        event.remove(reader.engine, "before_cursor_execute", interleave)
        if readonly:
            reader.close()


def test_report_loads_run_and_snapshot_once(setup, monkeypatch):
    app, provider, raw, _ = setup
    provider.data.update({f"S{i}": raw for i in range(30)})
    watchlist = app.watchlists.import_content(
        "thirty", "\n".join(f"S{i}" for i in range(30)).encode()
    )
    run = app.scans.scan(watchlist.id)
    calls = {"run": 0, "snapshot": 0}
    get_run, read = app.repository.run, app.snapshots.read

    def counted_run(identity):
        calls["run"] += 1
        return get_run(identity)

    def counted_read(digest):
        calls["snapshot"] += 1
        return read(digest)

    monkeypatch.setattr(app.repository, "run", counted_run)
    monkeypatch.setattr(app.snapshots, "read", counted_read)
    report = app.reports.build(run.id)
    assert len(report.charts) == 30
    assert calls == {"run": 1, "snapshot": 1}


def test_old_engine_is_readable_but_not_exactly_replayable(setup):
    from qscan.adapters.report_renderer import render
    from qscan.application.contracts import ApplicationError

    app, *_ = setup
    _, _, run = pair(setup)
    snapshot = app.snapshots.read(run.input_hash)
    context = snapshot.context.model_copy(update={"engine_version": "historical-engine"})
    historical = snapshot.model_copy(update={"context": context})
    digest = app.snapshots.write(historical)
    identity = uuid4()
    old_run = run.model_copy(update={"id": identity, "context": context, "input_hash": digest})
    app.repository.create_run(old_run.model_copy(update={"state": RunState.RUNNING, "results": ()}))
    app.repository.publish(old_run)
    content = app.snapshots.path(digest).read_bytes()
    report = app.reports.build(identity)
    assert report.charts and report.chart_error is None
    assert b"historical-engine" in render(report, "html")
    before = app.repository.summaries(None)
    with pytest.raises(ApplicationError, match="original engine"):
        app.scans.replay(identity)
    assert app.repository.summaries(None) == before
    assert app.snapshots.path(digest).read_bytes() == content


def test_csv_skips_snapshot_and_preserves_window_reasons(setup, monkeypatch, tmp_path):
    import csv
    import io
    import json

    from qscan.adapters.report_renderer import export, render
    from qscan.application.reporting import reason_details

    app, *_ = setup
    _, _, run = pair(setup)

    def forbidden(*args):
        raise AssertionError("CSV must not decode snapshots or calculate chart data")

    monkeypatch.setattr(app.snapshots, "read", forbidden)
    monkeypatch.setattr(app.reports, "chart_data", forbidden)
    report = app.reports.build(run.id, include_charts=False)
    assert not report.charts_included and not report.charts
    result = run.results[0]
    evidence = reason_details(result)
    row = next(csv.DictReader(io.StringIO(render(report, "csv").decode("utf-8-sig"))))
    assert json.loads(row["selected_window_reasons"]) == [
        r.model_dump() for r in evidence.selected_window_reasons
    ]
    assert json.loads(row["window_reasons"]) == [
        w.model_dump(mode="json") for w in evidence.windows
    ]
    assert any(w.reasons for w in evidence.windows)
    export(report, tmp_path, "csv")
    summary = json.loads((tmp_path / f"scan-{run.id}.summary.json").read_text(encoding="utf-8"))
    assert summary["run"]["counts"] == run.counts.model_dump()
    assert "results" not in summary["run"]


@pytest.mark.parametrize("case", ["zero", "partial", "unavailable"])
def test_empty_csv_has_real_summary(setup, tmp_path, case):
    import csv
    import json

    from qscan.adapters.report_renderer import export
    from qscan.application.contracts import ApplicationError, RawPrices
    from qscan.domain.models import ErrorCode

    app, provider, raw, _ = setup
    provider.data["FLAT"] = RawPrices(tuple((s, 99.0 + i % 2) for i, (s, _) in enumerate(raw.rows)))
    provider.data["BAD"] = ApplicationError(ErrorCode.NO_DATA)
    content = {"zero": b"FLAT", "partial": b"FLAT\nBAD", "unavailable": b"BAD"}[case]
    watchlist = app.watchlists.import_content(case, content)
    run = app.scans.scan(watchlist.id)
    path = export(app.reports.build(run.id, include_charts=False), tmp_path, "csv")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.DictReader(stream)) == []
    summary = json.loads(path.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert summary["run"]["state"] == run.state.value
    assert summary["run"]["counts"] == run.counts.model_dump()
    assert run.counts.evaluated == (0 if case == "unavailable" else 1)
    assert run.counts.data_error == (0 if case == "zero" else 1)


def test_normal_yahoo_metadata_cache_and_replay_are_offline(setup, monkeypatch):
    import pandas as pd

    from qscan.adapters.providers import YahooProvider
    from qscan.bootstrap import bootstrap
    from qscan.domain.models import DataMode

    fixture, _, raw, clock = setup
    calls = []

    def metadata(symbol, timeout):
        calls.append(("metadata", symbol, timeout))
        return {
            "symbol": symbol,
            "exchangeName": "NMS",
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
            "instrumentType": "EQUITY",
        }

    def download(symbol, start, end, timeout):
        calls.append(("download", symbol, timeout))
        return pd.DataFrame(
            {"Close": [c for _, c in raw.rows]}, index=pd.DatetimeIndex([s for s, _ in raw.rows])
        )

    provider = YahooProvider(metadata_loader=metadata, downloader=download)
    app = bootstrap(provider, data_dir=fixture.data_dir / "yahoo", clock=clock)
    try:
        watchlist = app.watchlists.import_content("actual-metadata", b"AAPL")
        assert watchlist.instruments[0].exchange == "UNKNOWN" and not calls
        run = app.scans.scan(watchlist.id)
        assert run.counts.evaluated == 1
        assert run.watchlist.instruments[0].exchange == "NASDAQ"
        assert run.watchlist.instruments[0].id == watchlist.instruments[0].id
        assert len(calls) == 2

        def forbidden(*args):
            raise AssertionError("offline path called network")

        monkeypatch.setattr(provider, "metadata_loader", forbidden)
        monkeypatch.setattr(provider, "downloader", forbidden)
        cached = app.scans.scan(watchlist.id, mode=DataMode.CACHE_ONLY)
        assert cached.results[0].analysis == run.results[0].analysis
        assert cached.results[0].instrument == run.results[0].instrument
        assert cached.results[0].provenance.cache
        assert app.scans.replay(run.id).results == run.results
        assert app.reports.build(run.id).charts
        assert app.repository.watchlist(watchlist.id) == watchlist
        hint = app.watchlists.import_content(
            "wrong-hint", b"symbol,exchange\nAAPL,NYSE", format="csv"
        )
        rejected = app.scans.scan(hint.id, mode=DataMode.CACHE_ONLY)
        assert rejected.counts.excluded == 1 and rejected.counts.evaluated == 0

    finally:
        app.close()


def test_unknown_rules_major_is_historical_only(setup):
    from qscan.application.contracts import ApplicationError

    app, *_ = setup
    _, _, run = pair(setup)
    snapshot = app.snapshots.read(run.input_hash)
    rules = snapshot.rules.model_copy(update={"version": "2.0.0"})
    digest = app.snapshots.write(snapshot.model_copy(update={"rules": rules}))
    old = run.model_copy(
        update={
            "id": uuid4(),
            "rules": rules,
            "config_hash": rules.config_hash(),
            "input_hash": digest,
        }
    )
    app.repository.create_run(old.model_copy(update={"state": RunState.RUNNING, "results": ()}))
    app.repository.publish(old)
    assert app.reports.build(old.id).charts
    with pytest.raises(ApplicationError, match="compatible rules"):
        app.scans.replay(old.id)


def test_json_stdout_is_utf8_decodable_on_legacy_codepage():
    import json
    import os
    import subprocess
    import sys

    value = {"output": "中文 path/報告.json", "warning": "測試"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; from qscan.interfaces.cli import emit; "
            "emit(json.loads(sys.argv[1]))",
            json.dumps(value),
        ],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("ascii", errors="replace")
    assert json.loads(result.stdout.decode("utf-8")) == value
