"""Explicit local dependency wiring; no transport or provider initialization side effects."""

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from filelock import FileLock
from platformdirs import user_data_path
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from qscan.adapters.calendar import NYSECalendar, SystemClock
from qscan.adapters.persistence.repository import SQLiteRepository, migrate, open_database
from qscan.adapters.snapshots import SnapshotStore
from qscan.application.contracts import (
    ApplicationContext,
    ApplicationError,
    Calendar,
    Clock,
    Provider,
)
from qscan.application.reporting import ComparisonService, ReportService
from qscan.application.services import (
    MarketDataService,
    ScanQueryService,
    ScanService,
    WatchlistService,
)
from qscan.domain.models import ErrorCode


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
    reports: ReportService
    comparisons: ComparisonService

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
    initialize: bool = True,
    readonly: bool = False,
) -> Application:
    try:
        directory = resolve_data_dir(data_dir)
    except ApplicationError as exc:
        raise ValueError(str(exc)) from exc
    if initialize:
        directory.mkdir(parents=True, exist_ok=True)
    elif not (directory / "qscan.sqlite3").is_file():
        raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Not initialized; run qscan init")
    lock = FileLock(directory / "executor.lock", timeout=10)
    clock = clock or SystemClock()
    calendar = calendar or NYSECalendar()
    if initialize:
        with lock:
            engine = open_database(directory / "qscan.sqlite3")
            migrate(engine)
    else:
        engine = open_database(directory / "qscan.sqlite3", readonly=readonly)
        try:
            with engine.connect() as connection:
                if connection.scalar(text("SELECT version_num FROM alembic_version")) != "0002":
                    raise ApplicationError(
                        ErrorCode.VALIDATION_ERROR, "Incompatible schema; run qscan init to upgrade"
                    )
        except ApplicationError:
            engine.dispose()
            raise
        except SQLAlchemyError as exc:
            engine.dispose()
            raise ApplicationError(
                ErrorCode.VALIDATION_ERROR, "Invalid database/schema; run qscan init"
            ) from exc
    repository = SQLiteRepository(engine, context or ApplicationContext(), clock, provider.name)
    snapshots = SnapshotStore(directory / "snapshots", create=initialize)
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
        ReportService(repository, snapshots),
        ComparisonService(repository, snapshots),
    )


def resolve_data_dir(data_dir: Path | None = None) -> Path:
    configured = data_dir or (
        Path(os.environ["QSCAN_DATA_DIR"])
        if os.environ.get("QSCAN_DATA_DIR")
        else user_data_path("qscan", appauthor=False)
    )
    if not configured.is_absolute():
        raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Data directory must be absolute")
    return configured.resolve()
