"""Durable serial batching for managed-universe runs."""

from datetime import UTC, date, datetime

from sqlalchemy import text

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import RawPrices, UniverseObservation
from qscan.bootstrap import bootstrap
from qscan.domain.models import RunState


class SyntheticUniverseSource:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols

    def fetch(self) -> UniverseObservation:
        return UniverseObservation(
            source_url="https://example.invalid/sp500",
            source_license="test fixture",
            source_revision="synthetic-503",
            observed_at=datetime(2026, 9, 5, tzinfo=UTC),
            symbols=self.symbols,
        )


def test_managed_run_checkpoints_503_members_in_eleven_chunks(tmp_path, monkeypatch):
    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    raw = RawPrices(tuple((session, 50.0 + index / 10) for index, session in enumerate(sessions)))
    symbols = tuple(f"S{index:03d}" for index in range(503))
    provider = FixtureProvider({symbol: raw for symbol in symbols})
    app = bootstrap(
        provider,
        data_dir=tmp_path / "resumable-503",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=calendar,
        universe_source=SyntheticUniverseSource(symbols),
    )
    commits: list[int] = []
    try:
        universe = app.universe.refresh()
        checkpoint_chunk = app.repository.checkpoint_chunk

        def counted_checkpoint(*args, **kwargs):
            checkpoint_chunk(*args, **kwargs)
            commits.append(kwargs["progress"].processed_symbols)

        monkeypatch.setattr(app.repository, "checkpoint_chunk", counted_checkpoint)
        run = app.scans.scan(universe.watchlist_id, as_of=date(2026, 9, 4))

        assert commits == [50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 503]
        assert run.counts.requested == run.counts.evaluated == 503
        assert len(run.results) == 503
        assert {result.instrument.display_symbol for result in run.results} >= {
            "S500",
            "S501",
            "S502",
        }
        snapshot = app.snapshots.read(run.input_hash or "")
        assert len(snapshot.items) == 503
        assert tuple(item.instrument.display_symbol for item in snapshot.items) == symbols
        assert len({result.rank for result in run.results if result.rank is not None}) == sum(
            result.rank is not None for result in run.results
        )
        assert app.repository.execution(run.id) is None
        with app.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM scan_input_checkpoints WHERE run_id = :run_id"),
                    {"run_id": str(run.id)},
                )
                == 0
            )
    finally:
        app.close()


def test_restart_resumes_same_run_after_150_without_refetch(tmp_path):
    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    raw = RawPrices(tuple((session, 60.0 + index / 10) for index, session in enumerate(sessions)))
    symbols = tuple(f"R{index:03d}" for index in range(503))
    provider = FixtureProvider({symbol: raw for symbol in symbols})
    directory = tmp_path / "restart-150"
    app = bootstrap(
        provider,
        data_dir=directory,
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=calendar,
        universe_source=SyntheticUniverseSource(symbols),
    )
    universe = app.universe.refresh()
    queued = app.scans.prepare(universe.watchlist_id, as_of=date(2026, 9, 4))
    app.repository.create_run(queued)
    assert (
        app.scans.execute_existing(queued.id, should_stop=lambda: len(provider.calls) >= 150)
        is None
    )
    interrupted = app.queries.get(queued.id)
    assert interrupted.state is RunState.RUNNING
    assert interrupted.progress is not None
    assert interrupted.progress.processed_symbols == 150
    frozen = app.repository.execution(queued.id)
    assert frozen is not None
    execution, items = frozen
    assert execution.next_index == len(items) == 150
    assert execution.calendar_version == calendar.version
    assert execution.provider == provider.name
    assert execution.engine_version == queued.context.engine_version
    assert execution.rules == queued.rules
    assert execution.config_hash == queued.config_hash
    assert execution.comparison == interrupted.comparison
    app.close()

    reopened = bootstrap(
        provider,
        data_dir=directory,
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=calendar,
        universe_source=SyntheticUniverseSource(symbols),
    )
    try:
        assert reopened.repository.recover_interrupted() == 1
        assert reopened.queries.get(queued.id).state is RunState.QUEUED
        completed = reopened.scans.execute_existing(queued.id)
        assert completed is not None
        assert completed.id == queued.id
        assert completed.state is RunState.SUCCEEDED
        fetched = [symbol for symbol, _, _ in provider.calls]
        assert len(fetched) == 503
        assert all(fetched.count(symbol) == 1 for symbol in symbols)
        assert len(reopened.queries.list()) == 1
    finally:
        reopened.close()


def test_corrupt_checkpoint_cursor_fails_safely_without_fetching(tmp_path):
    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    raw = RawPrices(tuple((session, 70.0 + index / 10) for index, session in enumerate(sessions)))
    symbols = tuple(f"C{index:03d}" for index in range(503))
    provider = FixtureProvider({symbol: raw for symbol in symbols})
    app = bootstrap(
        provider,
        data_dir=tmp_path / "corrupt-checkpoint",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=calendar,
        universe_source=SyntheticUniverseSource(symbols),
    )
    try:
        universe = app.universe.refresh()
        queued = app.scans.prepare(universe.watchlist_id, as_of=date(2026, 9, 4))
        app.repository.create_run(queued)
        app.scans.execute_existing(queued.id, should_stop=lambda: len(provider.calls) >= 50)
        with app.engine.begin() as connection:
            connection.execute(
                text("UPDATE scan_executions SET next_index = 51 WHERE run_id = :run_id"),
                {"run_id": str(queued.id)},
            )
        before = len(provider.calls)

        assert app.repository.recover_interrupted() == 1
        failed = app.queries.get(queued.id)
        assert failed.state is RunState.FAILED
        assert failed.error == "WORKER_INTERRUPTED"
        assert any("invalid resumable checkpoint" in warning for warning in failed.warnings)
        assert len(provider.calls) == before
    finally:
        app.close()


def test_legacy_running_recovery_remains_worker_interrupted(tmp_path):
    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    raw = RawPrices(tuple((session, 80.0 + index / 10) for index, session in enumerate(sessions)))
    provider = FixtureProvider({"MANUAL": raw})
    app = bootstrap(
        provider,
        data_dir=tmp_path / "legacy-recovery",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=calendar,
    )
    try:
        watchlist = app.watchlists.create("manual", ["MANUAL"])
        queued = app.scans.prepare(watchlist.id, as_of=date(2026, 9, 4))
        running = queued.model_copy(
            update={
                "state": RunState.RUNNING,
                "started_at": datetime(2026, 9, 5, 12, tzinfo=UTC),
            }
        )
        app.repository.create_run(running)

        assert app.repository.recover_interrupted() == 1
        failed = app.queries.get(queued.id)
        assert failed.state is RunState.FAILED
        assert failed.error == "WORKER_INTERRUPTED"
        assert failed.counts.data_error == 1
        assert app.repository.execution(queued.id) is None
    finally:
        app.close()
