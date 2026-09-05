"""Explicit local dependency wiring; no transport or provider initialization side effects."""

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from filelock import FileLock
from platformdirs import user_data_path
from sqlalchemy import Engine

from qscan.adapters.calendar import NYSECalendar, SystemClock
from qscan.adapters.persistence.repository import SQLiteRepository, migrate, open_database
from qscan.adapters.snapshots import SnapshotStore
from qscan.application.contracts import ApplicationContext, Calendar, Clock, Provider
from qscan.application.services import (
    MarketDataService,
    ScanQueryService,
    ScanService,
    WatchlistService,
)


@dataclass
class Application:
    data_dir: Path
    engine: Engine
    repository: SQLiteRepository
    snapshots: SnapshotStore
    watchlists: WatchlistService
    market: MarketDataService
    scans: ScanService
    queries: ScanQueryService

    def close(self) -> None:
        self.engine.dispose()


def bootstrap(
    provider: Provider,
    *,
    data_dir: Path | None = None,
    context: ApplicationContext | None = None,
    clock: Clock | None = None,
    calendar: Calendar | None = None,
    review_interval: timedelta = timedelta(days=30),
) -> Application:
    configured = data_dir or (
        Path(os.environ["QSCAN_DATA_DIR"])
        if os.environ.get("QSCAN_DATA_DIR")
        else user_data_path("qscan", appauthor=False)
    )
    if not configured.is_absolute():
        raise ValueError("Data directory must be absolute")
    directory = configured.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(directory / "executor.lock", timeout=10)
    clock = clock or SystemClock()
    calendar = calendar or NYSECalendar()
    with lock:
        engine = open_database(directory / "qscan.sqlite3")
        migrate(engine)
    repository = SQLiteRepository(engine, context or ApplicationContext(), clock)
    snapshots = SnapshotStore(directory / "snapshots")
    market = MarketDataService(repository, provider, clock, lock, review_interval)
    return Application(
        directory,
        engine,
        repository,
        snapshots,
        WatchlistService(repository, provider, lock),
        market,
        ScanService(repository, market, calendar, clock, snapshots, lock),
        ScanQueryService(repository),
    )
