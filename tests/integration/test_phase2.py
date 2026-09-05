import gzip
import hashlib
import json
import socket
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.persistence.repository import migrate
from qscan.adapters.providers import FixtureProvider, YahooProvider
from qscan.application.contracts import ApplicationContext, ApplicationError, RawPrices
from qscan.bootstrap import bootstrap
from qscan.domain.models import DataMode, ErrorCode, RunState
from qscan.domain.rules import RuleConfig


@pytest.fixture(scope="module")
def calendar():
    return NYSECalendar()


@pytest.fixture
def setup(tmp_path, calendar):
    sessions = calendar.sessions(date(2024, 1, 1), date(2026, 9, 4))[-504:]
    closes = [20 + i * 0.08 for i in range(375)] + [50 + i * 0.5 for i in range(88)]
    closes += [99, 100] * 20 + [100]
    raw = RawPrices(tuple(zip(sessions, closes, strict=True)))
    provider = FixtureProvider({"GOOD": raw})
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    app = bootstrap(provider, data_dir=tmp_path / "資料 space", clock=clock, calendar=calendar)
    yield app, provider, raw, clock
    app.close()


def test_migration_roundtrip_owner_and_reopen(setup, calendar):
    app, provider, raw, clock = setup
    migrate(app.engine)
    assert set(inspect(app.engine).get_table_names()) == {
        "alembic_version",
        "instruments",
        "prices",
        "watchlists",
        "watchlist_members",
        "scan_runs",
        "scan_results",
    }
    with app.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
        assert connection.scalar(text("PRAGMA busy_timeout")) == 10000
    watchlist = app.watchlists.import_content("test", b"GOOD")
    run = app.scans.scan(watchlist.id)
    migrate(app.engine)
    assert app.repository.watchlist(watchlist.id) == watchlist
    assert app.queries.get(run.id) == run
    second = bootstrap(
        provider,
        data_dir=app.data_dir,
        clock=clock,
        calendar=calendar,
        context=ApplicationContext("second"),
    )
    try:
        assert second.queries.list() == ()
        assert second.repository.watchlists() == ()
        for operation in (
            lambda: second.queries.get(run.id),
            lambda: second.repository.watchlist(watchlist.id),
            lambda: second.scans.replay(run.id),
            lambda: second.watchlists.import_content(
                "x", b"GOOD", watchlist_id=watchlist.id, expected_revision=1
            ),
        ):
            with pytest.raises(ApplicationError) as exc:
                operation()
            assert exc.value.code == ErrorCode.NOT_FOUND
        own = second.watchlists.import_content("test", b"GOOD")
        own_run = second.scans.scan(own.id, mode=DataMode.CACHE_ONLY)
        with pytest.raises(ApplicationError):
            app.queries.get(own_run.id)
    finally:
        second.close()
    app.close()
    reopened = bootstrap(provider, data_dir=app.data_dir, clock=clock, calendar=calendar)
    try:
        assert reopened.queries.get(run.id) == run
        assert reopened.scans.replay(run.id).results == run.results
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "format,content",
    [
        ("txt", "\ufeff # note\n good \nGOOD\n\nBRK.B\nbrk-b\n"),
        ("csv", "\ufeffsymbol,exchange\ngood,NYSE\nGOOD,NYSE\nBRK.B,NYSE\nBRK-B,NYSE\n"),
    ],
)
def test_import_bom_aliases_and_revision(setup, format, content):
    app, *_ = setup
    watchlist = app.watchlists.import_content("test", content.encode(), format)
    assert len(watchlist.instruments) == 2
    assert watchlist.instruments[1].provider_symbol == "BRK-B"
    changed = app.watchlists.import_content(
        "test", b"NEW", watchlist_id=watchlist.id, expected_revision=1
    )
    assert changed.revision == 2
    with pytest.raises(ApplicationError) as exc:
        app.watchlists.import_content(
            "test", b"OLD", watchlist_id=watchlist.id, expected_revision=1
        )
    assert exc.value.code == ErrorCode.WATCHLIST_VERSION_CONFLICT


@pytest.mark.parametrize(
    "content,format",
    [
        (b"x" * 1_000_001, "txt"),
        (b"", "txt"),
        (b"a\xff", "txt"),
        (b"../escape", "txt"),
        (b"ticker\nGOOD", "csv"),
        ("\n".join(f"S{i}" for i in range(2001)).encode(), "txt"),
        (b"symbol,exchange\nGOOD,NYSE\nGOOD,NASDAQ", "csv"),
    ],
    ids=["body_limit", "empty", "invalid_utf8", "path", "header", "symbol_limit", "exchange"],
)
def test_import_limits(setup, content, format):
    app, *_ = setup
    with pytest.raises(ApplicationError) as exc:
        app.watchlists.import_content("test", content, format)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert app.repository.watchlists() == ()


def test_partial_excluded_counts_and_all_failed(setup):
    app, provider, raw, _ = setup
    provider.data["SHORT"] = RawPrices(raw.rows[-50:])
    watchlist = app.watchlists.import_content("test", b"GOOD\nFAIL\nSHORT\nBTC-USD")
    run = app.scans.scan(watchlist.id)
    assert run.state == RunState.PARTIAL
    assert run.counts.model_dump() == dict(
        requested=4, evaluated=1, excluded=2, data_error=1, candidate=1
    )
    fail = app.watchlists.import_content("fail", b"SHORT\nFAIL")
    assert app.scans.scan(fail.id).state == RunState.FAILED
    unsupported = app.watchlists.import_content("other", b"symbol,exchange\nFOO,LSE", "csv")
    run = app.scans.scan(unsupported.id)
    assert run.state == RunState.FAILED and run.counts.excluded == 1


def test_zero_candidates_succeeds(setup):
    app, provider, raw, _ = setup
    provider.data["GOOD"] = RawPrices(
        tuple((s, 100 - i * 0.05) for i, (s, _) in enumerate(raw.rows))
    )
    watchlist = app.watchlists.import_content("test", b"GOOD")
    run = app.scans.scan(watchlist.id)
    assert run.state == RunState.SUCCEEDED
    assert run.counts.evaluated == 1 and run.counts.candidate == 0


def test_cache_only_and_replay_whole_path_no_network(setup, monkeypatch, calendar):
    app, provider, raw, clock = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    original = app.scans.scan(watchlist.id)

    def forbidden(*args, **kwargs):
        raise AssertionError("Network/provider access forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(provider, "fetch", forbidden)
    app.close()
    reopened = bootstrap(provider, data_dir=app.data_dir, clock=clock, calendar=calendar)
    try:
        imported = reopened.watchlists.import_content("offline", b"GOOD\nMISSING")
        run = reopened.scans.scan(imported.id, mode=DataMode.CACHE_ONLY)
        assert run.state == RunState.PARTIAL
        assert run.results[0].analysis == original.results[0].analysis
        monkeypatch.setattr(provider, "resolve", forbidden)
        assert reopened.scans.replay(original.id).results == original.results
    finally:
        reopened.close()
    yahoo = bootstrap(
        YahooProvider(), data_dir=app.data_dir / "yahoo", clock=clock, calendar=calendar
    )
    try:
        watchlist = yahoo.watchlists.import_content("offline", b"BRK.B")
        assert yahoo.scans.scan(watchlist.id, mode=DataMode.CACHE_ONLY).state == RunState.FAILED
    finally:
        yahoo.close()


def test_old_snapshot_survives_changes(setup):
    app, provider, raw, _ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    run = app.scans.scan(watchlist.id)
    provider.data["GOOD"] = RawPrices(tuple((s, c * 2) for s, c in raw.rows))
    updated = app.scans.scan(watchlist.id, mode=DataMode.FORCE, rules=RuleConfig(version="1.0.1"))
    assert updated.input_hash != run.input_hash
    app.watchlists.import_content("test", b"OTHER", watchlist_id=watchlist.id, expected_revision=1)
    with app.engine.begin() as connection:
        connection.execute(text("DELETE FROM prices"))
    replay = app.scans.replay(run.id)
    assert replay.id != run.id and replay.source_run_id == run.id
    assert replay.input_hash == run.input_hash and replay.results == run.results
    assert app.queries.get(run.id) == run


@pytest.mark.parametrize("damage", ["missing", "corrupt", "hash", "schema", "engine"])
def test_snapshot_damage_fails_explicitly(setup, damage):
    app, *_ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    run = app.scans.scan(watchlist.id)
    path = app.snapshots.path(run.input_hash)
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"broken")
    elif damage == "hash":
        path.write_bytes(gzip.compress(b"{}"))
    else:
        content = json.loads(gzip.decompress(path.read_bytes()))
        if damage == "schema":
            content["schema_version"] = 99
        else:
            content["context"]["engine_version"] = "incompatible"
        payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(payload).hexdigest()
        app.snapshots.path(digest).write_bytes(gzip.compress(payload))
        with pytest.raises(ApplicationError):
            app.snapshots.read(digest)
        return
    with pytest.raises(ApplicationError) as exc:
        app.scans.replay(run.id)
    assert exc.value.code == ErrorCode.SCAN_FAILED
    assert len(app.queries.list()) == 1


@pytest.mark.parametrize("failure", ["snapshot", "publication"])
def test_publication_failures_never_success(setup, monkeypatch, failure):
    app, *_ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    if failure == "snapshot":

        def broken(snapshot):
            raise OSError("Disk full")

        monkeypatch.setattr(app.snapshots, "write", broken)
    else:

        def reject(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO scan_results"):
                raise OSError("Injected publication failure")

        event.listen(app.engine, "before_cursor_execute", reject)
    with pytest.raises(OSError):
        app.scans.scan(watchlist.id)
    run = app.queries.list()[0]
    assert run.state == RunState.FAILED and run.results == () and run.input_hash is None
    assert run.counts.data_error == run.counts.requested


def test_foreign_keys_and_unique_constraints(setup):
    app, *_ = setup
    with pytest.raises(IntegrityError), app.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO watchlist_members VALUES (:w,:i,0,'{}')"),
            {"w": str(uuid4()), "i": str(uuid4())},
        )
    app.watchlists.import_content("same", b"GOOD")
    with pytest.raises(ApplicationError):
        app.watchlists.import_content("same", b"GOOD")


def test_provider_and_core_do_not_hold_write_transaction(setup, monkeypatch):
    app, provider, *_ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    original = provider.fetch

    def checked(*args):
        with app.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.rollback()
        return original(*args)

    monkeypatch.setattr(provider, "fetch", checked)
    import qscan.application.services as services

    core = services.analyze_symbol

    def checked_core(*args):
        with app.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.rollback()
        return core(*args)

    monkeypatch.setattr(services, "analyze_symbol", checked_core)
    assert app.scans.scan(watchlist.id).state == RunState.SUCCEEDED


def test_overlap_revision_full_replacement_and_failure_quarantine(setup, monkeypatch, calendar):
    app, provider, raw, _ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    first = app.scans.scan(watchlist.id, as_of=raw.rows[-2][0])
    original_fetch = provider.fetch
    revised = RawPrices(tuple((s, c / 2) for s, c in raw.rows), split_sessions=(raw.rows[-1][0],))
    provider.data["GOOD"] = revised
    second = app.scans.scan(watchlist.id)
    assert len(provider.calls) == 3
    assert provider.calls[1][1] == raw.rows[-11][0]
    assert provider.calls[2][1] < provider.calls[1][1]
    snapshot = app.snapshots.read(second.input_hash)
    assert all(
        a == b / 2 for a, (_, b) in zip(snapshot.items[0].series.closes, raw.rows, strict=True)
    )
    assert snapshot.items[0].provenance.revisions
    assert app.scans.replay(first.id).results == first.results
    # Re-prime yesterday, then fail the full refresh after detecting revised overlap.
    provider.data["GOOD"] = raw
    app.scans.scan(watchlist.id, as_of=raw.rows[-2][0], mode=DataMode.FORCE)
    provider.data["GOOD"] = revised

    def fail_full(instrument, start, end):
        if start < raw.rows[-11][0]:
            raise ApplicationError(ErrorCode.NO_DATA)
        return original_fetch(instrument, start, end)

    monkeypatch.setattr(provider, "fetch", fail_full)
    failed = app.scans.scan(watchlist.id)
    assert failed.state == RunState.FAILED
    assert failed.results[0].analysis.reasons[0].code == ErrorCode.ADJUSTMENT_REVIEW_REQUIRED
    assert not app.repository.cache(watchlist.instruments[0]).trusted
    assert (
        app.scans.scan(watchlist.id, as_of=raw.rows[-2][0], mode=DataMode.CACHE_ONLY).state
        == RunState.FAILED
    )


def test_refresh_warning_and_periodic_review(setup):
    app, provider, raw, clock = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    first = app.scans.scan(watchlist.id)
    app.scans.scan(watchlist.id)
    assert len(provider.calls) == 1
    provider.data["GOOD"] = ApplicationError(ErrorCode.NO_DATA)
    second = app.scans.scan(watchlist.id, mode=DataMode.FORCE)
    assert second.state == RunState.SUCCEEDED
    assert second.results[0].provenance.warnings == ("refresh_failed:NO_DATA",)
    assert app.repository.cache(watchlist.instruments[0]).trusted
    provider.data["GOOD"] = raw
    clock.instant += timedelta(days=31)
    app.scans.scan(watchlist.id, as_of=date(2026, 9, 4))
    assert provider.calls[-1][1] == raw.rows[0][0]
    assert app.scans.replay(first.id).results == first.results


def test_incremental_without_revision_keeps_retention(setup):
    app, provider, raw, _ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    app.scans.scan(watchlist.id, as_of=raw.rows[-2][0])
    run = app.scans.scan(watchlist.id)
    assert len(provider.calls) == 2
    cache = app.repository.cache(watchlist.instruments[0])
    assert len(cache.series.sessions) == 504
    assert tuple(zip(cache.series.sessions, cache.series.closes, strict=True)) == raw.rows
    assert run.state == RunState.SUCCEEDED


def test_unexpected_symbol_failure_does_not_stop_others(setup, monkeypatch):
    app, provider, *_ = setup
    original = provider.fetch

    def broken(instrument, start, end):
        if instrument.display_symbol == "BROKEN":
            raise RuntimeError("Unexpected provider shape")
        return original(instrument, start, end)

    monkeypatch.setattr(provider, "fetch", broken)
    watchlist = app.watchlists.import_content("test", b"BROKEN\nGOOD")
    run = app.scans.scan(watchlist.id)
    assert run.state == RunState.PARTIAL and run.counts.evaluated == 1
    assert run.results[-1].analysis.reasons[0].code == ErrorCode.INTERNAL_ERROR


def test_cache_atomic_replace_and_failed_full_revision(setup):
    app, provider, raw, _ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    app.scans.scan(watchlist.id)
    cached = app.repository.cache(watchlist.instruments[0])

    def reject(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO prices"):
            raise OSError("Disk error during replacement")

    event.listen(app.engine, "before_cursor_execute", reject)
    with pytest.raises(OSError):
        app.scans.scan(watchlist.id, mode=DataMode.FORCE)
    assert app.repository.cache(watchlist.instruments[0]) == cached
    event.remove(app.engine, "before_cursor_execute", reject)
    # Revision is visible before invalid latest Close: the old cache is no longer trusted.
    provider.data["GOOD"] = RawPrices(
        tuple((s, c / 2) for s, c in raw.rows[:-1]) + ((raw.rows[-1][0], float("nan")),)
    )
    failed = app.scans.scan(watchlist.id, mode=DataMode.FORCE)
    assert failed.state == RunState.FAILED
    retained = app.repository.cache(watchlist.instruments[0])
    assert retained.series == cached.series and not retained.trusted


def test_executor_collision_and_absolute_path(setup):
    from pathlib import Path

    from filelock import FileLock, Timeout

    app, provider, *_ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    app.scans.lock.timeout = 0.01
    with FileLock(app.data_dir / "executor.lock"), pytest.raises(Timeout):
        app.scans.scan(watchlist.id)
    assert app.queries.list() == ()
    with pytest.raises(ValueError, match="absolute"):
        bootstrap(provider, data_dir=Path("relative"))


def test_input_hash_is_content_addressed_and_no_private_path(setup):
    app, *_ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")
    run = app.scans.scan(watchlist.id)
    payload = gzip.decompress(app.snapshots.path(run.input_hash).read_bytes())
    assert hashlib.sha256(payload).hexdigest() == run.input_hash
    assert "snapshot_path" not in run.model_dump_json()
    assert str(app.data_dir) not in run.model_dump_json()
    assert set(run.timings) == {"market_validation", "snapshot", "core", "db_publication_precommit"}
    assert all(t >= 0 for t in run.timings.values())


def test_fetched_at_records_response_completion(setup, monkeypatch):
    app, provider, _, clock = setup
    original = provider.fetch

    def delayed(*args):
        clock.instant += timedelta(seconds=15)
        return original(*args)

    monkeypatch.setattr(provider, "fetch", delayed)
    watchlist = app.watchlists.import_content("test", b"GOOD")
    run = app.scans.scan(watchlist.id)
    assert run.results[0].provenance.fetched_at == clock.instant


def test_atomic_rename_failure_leaves_no_snapshot_reference(setup, monkeypatch):
    app, *_ = setup
    watchlist = app.watchlists.import_content("test", b"GOOD")

    def reject(*args):
        raise OSError("Atomic rename failed")

    monkeypatch.setattr("qscan.adapters.snapshots.os.replace", reject)
    with pytest.raises(OSError):
        app.scans.scan(watchlist.id)
    assert list(app.snapshots.directory.iterdir()) == []
    assert app.queries.list()[0].state == RunState.FAILED
    assert app.queries.list()[0].input_hash is None


def test_partial_inserts_are_not_visible_and_roll_back(setup):
    app, provider, raw, _ = setup
    provider.data["SECOND"] = raw
    watchlist = app.watchlists.import_content("test", b"GOOD\nSECOND")
    inserted = 0

    def reject(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserted
        if statement.startswith("INSERT INTO scan_results"):
            inserted += 1
            visible = app.queries.list()[0]
            assert visible.state == RunState.RUNNING and visible.results == ()
            if inserted == 2:
                raise OSError("Second result write failed")

    event.listen(app.engine, "before_cursor_execute", reject)
    with pytest.raises(OSError):
        app.scans.scan(watchlist.id)
    assert inserted == 2
    assert app.queries.list()[0].state == RunState.FAILED
    assert app.queries.list()[0].results == ()


def test_csv_missing_cell_is_typed_validation_failure(setup):
    app, *_ = setup
    with pytest.raises(ApplicationError) as exc:
        app.watchlists.import_content("test", b"exchange,symbol\nNYSE", "csv")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
