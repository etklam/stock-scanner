"""Owner-scoped repositories; every operation owns a short-lived connection."""

from datetime import UTC, date
from pathlib import Path
from time import perf_counter
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Connection, Engine, create_engine, delete, event, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from qscan.adapters.persistence.migrations.schema_v1 import (
    instruments,
    members,
    prices,
    results,
    runs,
    watchlists,
)
from qscan.application.contracts import (
    ApplicationContext,
    ApplicationError,
    CacheEntry,
    Clock,
    Instrument,
    Provenance,
    Run,
    ScanResult,
    Watchlist,
)
from qscan.domain.models import CloseSeries, ErrorCode, RunState


def open_database(path: Path) -> Engine:
    engine = create_engine(URL.create("sqlite", database=str(path)), connect_args={"timeout": 10})

    @event.listens_for(engine, "connect")
    def configure(connection: object, record: object) -> None:
        # The DBAPI connection is sqlite3.Connection, not an ORM Session.
        import sqlite3

        assert isinstance(connection, sqlite3.Connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")

    return engine


def migrate(engine: Engine) -> None:
    config = Config()
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


class SQLiteRepository:
    def __init__(self, engine: Engine, context: ApplicationContext, clock: Clock) -> None:
        self.engine, self.context, self.clock = engine, context, clock

    def _instrument(self, connection: Connection, value: Instrument) -> None:
        connection.execute(
            sqlite_insert(instruments)
            .values(
                id=str(value.id),
                provider_symbol=value.provider_symbol,
                market="US",
                document=value.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )

    def save_watchlist(self, value: Watchlist, expected_revision: int | None) -> None:
        now = self.clock.now().astimezone(UTC).isoformat()
        try:
            with self.engine.begin() as connection:
                if expected_revision is None:
                    connection.execute(
                        insert(watchlists).values(
                            id=str(value.id),
                            owner_id=self.context.principal,
                            name=value.name,
                            revision=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    changed = connection.execute(
                        update(watchlists)
                        .where(
                            watchlists.c.id == str(value.id),
                            watchlists.c.owner_id == self.context.principal,
                            watchlists.c.revision == expected_revision,
                        )
                        .values(name=value.name, revision=value.revision, updated_at=now)
                    )
                    if changed.rowcount != 1:
                        raise ApplicationError(ErrorCode.WATCHLIST_VERSION_CONFLICT)
                    connection.execute(
                        delete(members).where(members.c.watchlist_id == str(value.id))
                    )
                for position, instrument in enumerate(value.instruments):
                    self._instrument(connection, instrument)
                    connection.execute(
                        insert(members).values(
                            watchlist_id=str(value.id),
                            instrument_id=str(instrument.id),
                            position=position,
                            document=instrument.model_dump(mode="json"),
                        )
                    )
        except IntegrityError as exc:
            raise ApplicationError(ErrorCode.WATCHLIST_VERSION_CONFLICT) from exc

    def watchlist(self, identity: UUID) -> Watchlist:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(watchlists).where(
                        watchlists.c.id == str(identity),
                        watchlists.c.owner_id == self.context.principal,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            documents = connection.execute(
                select(members.c.document)
                .where(members.c.watchlist_id == str(identity))
                .order_by(members.c.position)
            ).scalars()
            return Watchlist(
                id=identity,
                name=row["name"],
                revision=row["revision"],
                instruments=tuple(Instrument.model_validate(d) for d in documents),
            )

    def watchlists(self) -> tuple[Watchlist, ...]:
        with self.engine.connect() as connection:
            identities = (
                connection.execute(
                    select(watchlists.c.id)
                    .where(watchlists.c.owner_id == self.context.principal)
                    .order_by(watchlists.c.name)
                )
                .scalars()
                .all()
            )
        return tuple(self.watchlist(UUID(i)) for i in identities)

    def cache(self, instrument: Instrument) -> CacheEntry | None:
        with self.engine.connect() as connection:
            info = connection.execute(
                select(instruments.c.cache_info).where(instruments.c.id == str(instrument.id))
            ).scalar_one_or_none()
            if info is None:
                return None
            rows = (
                connection.execute(
                    select(prices)
                    .where(prices.c.instrument_id == str(instrument.id))
                    .order_by(prices.c.session)
                )
                .mappings()
                .all()
            )
            if not rows:
                return None
            return CacheEntry(
                series=CloseSeries(
                    instrument_id=instrument.id,
                    sessions=tuple(date.fromisoformat(r["session"]) for r in rows),
                    closes=tuple(r["close"] for r in rows),
                ),
                provenance=Provenance.model_validate(info["provenance"]),
                reviewed_at=info["reviewed_at"],
                trusted=info["trusted"],
            )

    def replace_cache(self, instrument: Instrument, value: CacheEntry) -> None:
        with self.engine.begin() as connection:
            self._instrument(connection, instrument)
            connection.execute(delete(prices).where(prices.c.instrument_id == str(instrument.id)))
            connection.execute(
                insert(prices),
                [
                    dict(
                        instrument_id=str(instrument.id),
                        session=session.isoformat(),
                        close=close,
                        price_basis=value.series.price_basis.value,
                        provider=value.provenance.provider,
                        fetched_at=value.provenance.fetched_at.isoformat(),
                    )
                    for session, close in zip(
                        value.series.sessions, value.series.closes, strict=True
                    )
                ],
            )
            connection.execute(
                update(instruments)
                .where(instruments.c.id == str(instrument.id))
                .values(cache_info=value.model_dump(mode="json", exclude={"series"}))
            )

    def invalidate_cache(self, instrument: Instrument, reason: str) -> None:
        cached = self.cache(instrument)
        if cached is not None:
            provenance = cached.provenance.model_copy(
                update={"revisions": (*cached.provenance.revisions, reason)}
            )
            with self.engine.begin() as connection:
                connection.execute(
                    update(instruments)
                    .where(instruments.c.id == str(instrument.id))
                    .values(
                        cache_info=cached.model_copy(
                            update={
                                "trusted": False,
                                "provenance": provenance,
                            }
                        ).model_dump(mode="json", exclude={"series"})
                    )
                )

    def create_run(self, run: Run) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(runs).values(
                    id=str(run.id),
                    owner_id=self.context.principal,
                    state=run.state.value,
                    created_at=run.requested_at.isoformat(),
                    source_run_id=str(run.source_run_id) if run.source_run_id else None,
                    document=run.model_dump(mode="json", exclude={"results"}),
                )
            )

    def publish(self, run: Run) -> None:
        started = perf_counter()
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(runs)
                .where(
                    runs.c.id == str(run.id),
                    runs.c.owner_id == self.context.principal,
                    runs.c.state == RunState.RUNNING.value,
                )
                .values(
                    state=run.state.value,
                    input_hash=run.input_hash,
                    document=run.model_dump(mode="json", exclude={"results"}),
                )
            )
            if changed.rowcount != 1:
                raise ApplicationError(ErrorCode.SCAN_NOT_READY)
            for result in run.results:
                connection.execute(
                    insert(results).values(
                        run_id=str(run.id),
                        instrument_id=str(result.instrument.id),
                        is_candidate=result.analysis.is_candidate,
                        score=result.analysis.score,
                        document=result.model_dump(mode="json"),
                    )
                )

            measured = run.model_copy(
                update={
                    "timings": {
                        **run.timings,
                        "db_publication_precommit": perf_counter() - started,
                    },
                    "finished_at": self.clock.now().astimezone(UTC),
                }
            )
            connection.execute(
                update(runs)
                .where(runs.c.id == str(run.id), runs.c.owner_id == self.context.principal)
                .values(document=measured.model_dump(mode="json", exclude={"results"}))
            )

    def fail(self, run: Run) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(runs)
                .where(
                    runs.c.id == str(run.id),
                    runs.c.owner_id == self.context.principal,
                    runs.c.state == RunState.RUNNING.value,
                )
                .values(
                    state=RunState.FAILED.value,
                    document=run.model_dump(mode="json", exclude={"results"}),
                )
            )

    def run(self, identity: UUID) -> Run:
        with self.engine.connect() as connection:
            document = connection.execute(
                select(runs.c.document).where(
                    runs.c.id == str(identity),
                    runs.c.owner_id == self.context.principal,
                )
            ).scalar_one_or_none()
            if document is None:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            rows = connection.execute(
                select(results.c.document)
                .join(runs, results.c.run_id == runs.c.id)
                .where(runs.c.id == str(identity), runs.c.owner_id == self.context.principal)
            ).scalars()
            values = sorted(
                (ScanResult.model_validate(r) for r in rows),
                key=lambda r: (r.rank is None, r.rank or 0, r.instrument.id),
            )
            return Run.model_validate({**document, "results": tuple(values)})

    def runs(self) -> tuple[Run, ...]:
        with self.engine.connect() as connection:
            identities = (
                connection.execute(
                    select(runs.c.id)
                    .where(runs.c.owner_id == self.context.principal)
                    .order_by(runs.c.created_at.desc(), runs.c.id.desc())
                )
                .scalars()
                .all()
            )
        return tuple(self.run(UUID(i)) for i in identities)
