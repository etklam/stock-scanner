"""Owner-scoped repositories; every operation owns a short-lived connection."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    URL,
    Connection,
    Engine,
    create_engine,
    delete,
    event,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from qscan.adapters.persistence.migrations.schema_v1 import (
    instruments,
    members,
    results,
    watchlists,
)
from qscan.adapters.persistence.migrations.schema_v2 import cache, prices
from qscan.adapters.persistence.migrations.schema_v3 import runs
from qscan.adapters.persistence.migrations.schema_v4 import reviews
from qscan.application.contracts import (
    ApplicationContext,
    ApplicationError,
    CacheEntry,
    Clock,
    Counts,
    Instrument,
    Progress,
    Provenance,
    Run,
    SavedReview,
    ScanResult,
    Watchlist,
)
from qscan.domain.models import CloseSeries, ErrorCode, RunState


def open_database(path: Path, *, readonly: bool = False) -> Engine:
    url = (
        URL.create("sqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"})
        if readonly
        else URL.create("sqlite", database=str(path))
    )
    engine = create_engine(url, connect_args={"timeout": 10})

    @event.listens_for(engine, "connect")
    def configure(connection: object, record: object) -> None:
        # The DBAPI connection is sqlite3.Connection, not an ORM Session.
        import sqlite3

        assert isinstance(connection, sqlite3.Connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        if not readonly:
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
    def __init__(
        self, engine: Engine, context: ApplicationContext, clock: Clock, provider: str = "fixture"
    ) -> None:
        self.engine, self.context, self.clock = engine, context, clock
        self.provider = provider

    def for_principal(self, principal: str) -> "SQLiteRepository":
        """Same engine/clock; ownership only decides row visibility."""
        if principal == self.context.principal:
            return self
        return SQLiteRepository(
            self.engine, ApplicationContext(principal), self.clock, self.provider
        )

    @contextmanager
    def _read(self) -> Iterator[Connection]:
        # sqlite3 legacy mode does not BEGIN for SELECT, even inside engine.begin().
        # Explicit BEGIN fixes one resource's multi-SELECT view; close rolls it back.
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            yield connection

    @contextmanager
    def _write(self) -> Iterator[Connection]:
        # BEGIN IMMEDIATE takes the SQLite write lock up front: concurrent capacity
        # checks and key reservations serialize instead of racing to upgrade.
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()

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
        with self._read() as connection:
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
        with self._read() as connection:
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

    def delete_watchlist(self, identity: UUID) -> None:
        with self._write() as connection:
            existing = connection.execute(
                select(watchlists.c.id).where(
                    watchlists.c.id == str(identity),
                    watchlists.c.owner_id == self.context.principal,
                )
            ).scalar_one_or_none()
            if existing is None:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            active = connection.execute(
                select(runs.c.id)
                .where(
                    runs.c.state.in_([RunState.QUEUED.value, RunState.RUNNING.value]),
                    func.json_extract(runs.c.document, "$.watchlist.id") == str(identity),
                )
                .limit(1)
            ).scalar_one_or_none()
            if active is not None:
                raise ApplicationError(ErrorCode.WATCHLIST_IN_USE)
            connection.execute(delete(members).where(members.c.watchlist_id == str(identity)))
            connection.execute(
                delete(watchlists).where(
                    watchlists.c.id == str(identity),
                    watchlists.c.owner_id == self.context.principal,
                )
            )

    def cache(self, instrument: Instrument) -> CacheEntry | None:
        with self._read() as connection:
            info = connection.execute(
                select(cache.c.document).where(
                    cache.c.instrument_id == str(instrument.id), cache.c.provider == self.provider
                )
            ).scalar_one_or_none()
            if info is None:
                return None
            rows = (
                connection.execute(
                    select(prices)
                    .where(
                        prices.c.instrument_id == str(instrument.id),
                        prices.c.provider == self.provider,
                    )
                    .order_by(prices.c.session)
                )
                .mappings()
                .all()
            )
            if not rows:
                return None
            return CacheEntry(
                instrument=Instrument.model_validate(info["instrument"])
                if info.get("instrument")
                else None,
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
        if value.provenance.provider != self.provider:
            raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Cache source mismatch")
        with self.engine.begin() as connection:
            self._instrument(connection, instrument)
            connection.execute(
                delete(prices).where(
                    prices.c.instrument_id == str(instrument.id), prices.c.provider == self.provider
                )
            )
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
                sqlite_insert(cache)
                .values(
                    instrument_id=str(instrument.id),
                    provider=self.provider,
                    document=value.model_dump(mode="json", exclude={"series"}),
                )
                .on_conflict_do_update(
                    index_elements=["instrument_id", "provider"],
                    set_={"document": value.model_dump(mode="json", exclude={"series"})},
                )
            )

    def invalidate_cache(self, instrument: Instrument, reason: str) -> None:
        cached = self.cache(instrument)
        if cached is not None:
            provenance = cached.provenance.model_copy(
                update={"revisions": (*cached.provenance.revisions, reason)}
            )
            with self.engine.begin() as connection:
                connection.execute(
                    update(cache)
                    .where(
                        cache.c.instrument_id == str(instrument.id),
                        cache.c.provider == self.provider,
                    )
                    .values(
                        document=cached.model_copy(
                            update={"trusted": False, "provenance": provenance}
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
                    idempotency_key=None,
                    request_hash=None,
                    document=run.model_dump(mode="json", exclude={"results"}),
                )
            )

    def enqueue(
        self, run: Run, idempotency_key: str, request_hash: str, queue_limit: int
    ) -> tuple[bool, tuple[UUID, str] | None]:
        """Reserve the key, check capacity, and insert the QUEUED run in one transaction."""
        try:
            with self._write() as connection:
                existing = connection.execute(
                    select(runs.c.id, runs.c.request_hash).where(
                        runs.c.owner_id == self.context.principal,
                        runs.c.idempotency_key == idempotency_key,
                    )
                ).first()
                if existing is not None:
                    return False, (UUID(existing.id), existing.request_hash or "")
                queued = connection.scalar(
                    select(func.count())
                    .select_from(runs)
                    .where(
                        runs.c.owner_id == self.context.principal,
                        runs.c.state == RunState.QUEUED.value,
                    )
                )
                if queued is not None and queued >= queue_limit:
                    raise ApplicationError(
                        ErrorCode.QUEUE_LIMIT_REACHED,
                        "Queued scan limit reached; wait for running scans to finish",
                    )
                connection.execute(
                    insert(runs).values(
                        id=str(run.id),
                        owner_id=self.context.principal,
                        state=run.state.value,
                        created_at=run.requested_at.isoformat(),
                        source_run_id=None,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        document=run.model_dump(mode="json", exclude={"results"}),
                    )
                )
                return True, None
        except IntegrityError as exc:
            # Concurrent insert of the same key: report the winner instead of guessing.
            with self._read() as connection:
                existing = connection.execute(
                    select(runs.c.id, runs.c.request_hash).where(
                        runs.c.owner_id == self.context.principal,
                        runs.c.idempotency_key == idempotency_key,
                    )
                ).first()
            if existing is None:
                raise ApplicationError(ErrorCode.INTERNAL_ERROR, "Queue insert conflict") from exc
            return False, (UUID(existing.id), existing.request_hash or "")

    def claim(self, run: Run) -> bool:
        """QUEUED -> RUNNING compare-and-set; only one executor can win."""
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(runs)
                .where(
                    runs.c.id == str(run.id),
                    runs.c.owner_id == self.context.principal,
                    runs.c.state == RunState.QUEUED.value,
                )
                .values(
                    state=run.state.value,
                    document=run.model_dump(mode="json", exclude={"results"}),
                )
            )
            return changed.rowcount == 1

    def update_progress(self, identity: UUID, progress: Progress) -> None:
        """Live progress only; never touches a terminal run."""
        with self.engine.begin() as connection:
            document = connection.execute(
                select(runs.c.document).where(
                    runs.c.id == str(identity),
                    runs.c.owner_id == self.context.principal,
                    runs.c.state == RunState.RUNNING.value,
                )
            ).scalar_one_or_none()
            if document is None:
                return
            run = Run.model_validate(document)
            connection.execute(
                update(runs)
                .where(
                    runs.c.id == str(identity),
                    runs.c.owner_id == self.context.principal,
                    runs.c.state == RunState.RUNNING.value,
                )
                .values(
                    document=run.model_copy(update={"progress": progress}).model_dump(
                        mode="json", exclude={"results"}
                    )
                )
            )

    def next_queued(self) -> UUID | None:
        """Stable FIFO (created_at, id) across all owners in this data directory."""
        with self._read() as connection:
            identity = connection.execute(
                select(runs.c.id)
                .where(runs.c.state == RunState.QUEUED.value)
                .order_by(runs.c.created_at.asc(), runs.c.id.asc())
                .limit(1)
            ).scalar_one_or_none()
            return UUID(identity) if identity else None

    def run_owner(self, identity: UUID) -> str | None:
        with self._read() as connection:
            return connection.execute(
                select(runs.c.owner_id).where(runs.c.id == str(identity))
            ).scalar_one_or_none()

    def run_by_idempotency(self, key: str) -> tuple[UUID, str] | None:
        with self._read() as connection:
            row = connection.execute(
                select(runs.c.id, runs.c.request_hash).where(
                    runs.c.owner_id == self.context.principal,
                    runs.c.idempotency_key == key,
                )
            ).first()
            return (UUID(row.id), row.request_hash or "") if row else None

    def recover_interrupted(self) -> int:
        """Mark leftover RUNNING rows FAILED/WORKER_INTERRUPTED; done runs are untouched.

        Recovered rows stay valid Run documents: live progress is cleared and
        counts are closed out like any other failure. An unreadable document
        aborts startup instead of persisting a document that no reader can parse.
        """
        recovered = 0
        with self._write() as connection:
            rows = connection.execute(
                select(runs.c.id, runs.c.document).where(runs.c.state == RunState.RUNNING.value)
            ).mappings()
            for row in rows:
                try:
                    run = Run.model_validate(row["document"])
                except ValueError as exc:
                    raise ApplicationError(
                        ErrorCode.INTERNAL_ERROR,
                        f"Unreadable run document {row['id']}; restore it from backup",
                    ) from exc
                document = run.model_copy(
                    update={
                        "state": RunState.FAILED,
                        "finished_at": self.clock.now().astimezone(UTC),
                        "error": ErrorCode.WORKER_INTERRUPTED,
                        "progress": None,
                        "counts": Counts(
                            requested=run.counts.requested, data_error=run.counts.requested
                        ),
                    }
                ).model_dump(mode="json", exclude={"results"})
                changed = connection.execute(
                    update(runs)
                    .where(runs.c.id == row["id"], runs.c.state == RunState.RUNNING.value)
                    .values(state=RunState.FAILED.value, document=document)
                )
                recovered += changed.rowcount
        return recovered

    def fail_queued(self, identity: UUID, error: ErrorCode, warnings: tuple[str, ...] = ()) -> bool:
        """QUEUED -> FAILED without executing (compatibility gate or poison job).

        Unfiltered by owner like the other queue operations; the CAS keeps it a
        no-op unless the row is still QUEUED.
        """
        with self._write() as connection:
            document = connection.execute(
                select(runs.c.document).where(
                    runs.c.id == str(identity), runs.c.state == RunState.QUEUED.value
                )
            ).scalar_one_or_none()
            if document is None:
                return False
            run = Run.model_validate(document)
            connection.execute(
                update(runs)
                .where(runs.c.id == str(identity), runs.c.state == RunState.QUEUED.value)
                .values(
                    state=RunState.FAILED.value,
                    document=run.model_copy(
                        update={
                            "state": RunState.FAILED,
                            "finished_at": self.clock.now().astimezone(UTC),
                            "error": error,
                            "progress": None,
                            # Nothing was fetched: requested is kept, coverage stays zero.
                            "counts": Counts(requested=run.counts.requested),
                            "warnings": warnings,
                        }
                    ).model_dump(mode="json", exclude={"results"}),
                )
            )
            return True

    def runs_page(
        self, *, after: tuple[str, str] | None, limit: int, state: str | None
    ) -> tuple[tuple[Run, ...], bool]:
        conditions = [runs.c.owner_id == self.context.principal]
        if state is not None:
            conditions.append(runs.c.state == state)
        if after is not None:
            conditions.append(
                or_(
                    runs.c.created_at < after[0],
                    (runs.c.created_at == after[0]) & (runs.c.id < after[1]),
                )
            )
        with self._read() as connection:
            documents = (
                connection.execute(
                    select(runs.c.document)
                    .where(*conditions)
                    .order_by(runs.c.created_at.desc(), runs.c.id.desc())
                    .limit(limit + 1)
                )
                .scalars()
                .all()
            )
        more = len(documents) > limit
        return tuple(Run.model_validate(d) for d in documents[:limit]), more

    def run_summary(self, identity: UUID) -> Run:
        with self._read() as connection:
            document = connection.execute(
                select(runs.c.document).where(
                    runs.c.id == str(identity),
                    runs.c.owner_id == self.context.principal,
                )
            ).scalar_one_or_none()
            if document is None:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            return Run.model_validate(document)

    def results_page(
        self,
        identity: UUID,
        *,
        after: tuple[int, int | None, str] | None,
        limit: int,
        stage: str | None,
        candidate: bool | None,
    ) -> tuple[tuple[ScanResult, ...], bool]:
        null_score = results.c.score.is_(None)
        conditions = [
            runs.c.id == str(identity),
            runs.c.owner_id == self.context.principal,
            results.c.run_id == runs.c.id,
        ]
        if stage is not None:
            conditions.append(func.json_extract(results.c.document, "$.analysis.stage") == stage)
        if candidate is not None:
            conditions.append(results.c.is_candidate == candidate)
        if after is not None:
            null_flag, cursor_score, instrument = after
            # Continuation for ORDER BY (score IS NULL) ASC, score DESC, instrument_id ASC:
            # keep only rows strictly after the cursor row under that exact ordering.
            if null_flag:
                conditions.append(null_score & (results.c.instrument_id > instrument))
            elif cursor_score is not None:
                conditions.append(
                    null_score
                    | (results.c.score < cursor_score)
                    | ((results.c.score == cursor_score) & (results.c.instrument_id > instrument))
                )
        with self._read() as connection:
            documents = (
                connection.execute(
                    select(results.c.document)
                    .join(runs, results.c.run_id == runs.c.id)
                    .where(*conditions)
                    .order_by(
                        null_score.asc(), results.c.score.desc(), results.c.instrument_id.asc()
                    )
                    .limit(limit + 1)
                )
                .scalars()
                .all()
            )
        more = len(documents) > limit
        values = tuple(ScanResult.model_validate(d) for d in documents[:limit])
        return values, more

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
        with self._read() as connection:
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
        with self._read() as connection:
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

    def summaries(self, limit: int | None = 30) -> tuple[Run, ...]:
        with self._read() as connection:
            statement = (
                select(runs.c.document)
                .where(runs.c.owner_id == self.context.principal)
                .order_by(runs.c.created_at.desc(), runs.c.id.desc())
            )
            if limit is not None:
                statement = statement.limit(limit)
            return tuple(Run.model_validate(d) for d in connection.execute(statement).scalars())

    # --- human review labels: mutable data, never part of scan results ---

    def reviews_for_run(self, identity: UUID) -> tuple[SavedReview, ...]:
        with self._read() as connection:
            rows = connection.execute(
                select(reviews)
                .where(
                    reviews.c.run_id == str(identity),
                    reviews.c.owner_id == self.context.principal,
                )
                .order_by(reviews.c.instrument_id)
            ).mappings()
            return tuple(
                SavedReview(
                    run_id=UUID(row["run_id"]),
                    instrument_id=UUID(row["instrument_id"]),
                    label=row["label"],
                    note=row["note"],
                    revision=row["revision"],
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
                for row in rows
            )

    def save_review(
        self,
        identity: UUID,
        instrument_id: UUID,
        label: str,
        note: str,
        expected_revision: int | None,
    ) -> SavedReview:
        """Insert or update one label with optimistic revision concurrency.

        A conflicting expected_revision is refused (never silently applied) so
        two editors cannot overwrite each other; every write bumps revision.
        """
        now = self.clock.now().astimezone(UTC).isoformat()
        with self._write() as connection:
            existing = connection.execute(
                select(reviews.c.revision).where(
                    reviews.c.run_id == str(identity),
                    reviews.c.instrument_id == str(instrument_id),
                    reviews.c.owner_id == self.context.principal,
                )
            ).scalar_one_or_none()
            if existing is not None and expected_revision is None:
                # A blind PUT against an existing label would be a silent
                # overwrite; require the current revision explicitly.
                raise ApplicationError(ErrorCode.REVIEW_REVISION_CONFLICT)
            if expected_revision is not None and existing != expected_revision:
                raise ApplicationError(ErrorCode.REVIEW_REVISION_CONFLICT)
            revision = (existing or 0) + 1
            connection.execute(
                sqlite_insert(reviews)
                .values(
                    owner_id=self.context.principal,
                    run_id=str(identity),
                    instrument_id=str(instrument_id),
                    label=label,
                    note=note,
                    revision=revision,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["owner_id", "run_id", "instrument_id"],
                    set_={"label": label, "note": note, "revision": revision, "updated_at": now},
                )
            )
            return SavedReview(
                run_id=identity,
                instrument_id=instrument_id,
                label=label,
                note=note,
                revision=revision,
                updated_at=datetime.fromisoformat(now),
            )
