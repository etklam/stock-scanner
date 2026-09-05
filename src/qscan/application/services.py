"""Synchronous watchlist, market data, scan and query use cases."""

import csv
import io
import math
import re
from datetime import UTC, date, timedelta
from time import perf_counter
from uuid import UUID, uuid4

from filelock import FileLock

from qscan.application.contracts import (
    ApplicationError,
    CacheEntry,
    Calendar,
    Clock,
    Counts,
    InputItem,
    InputSnapshot,
    Instrument,
    Provenance,
    Provider,
    RawPrices,
    RefreshItem,
    RefreshResult,
    Repository,
    Run,
    ScanResult,
    Snapshots,
    Watchlist,
)
from qscan.application.quality import validate
from qscan.core import analyze_symbol, rank_candidates
from qscan.domain.analysis import Reason, SymbolAnalysis
from qscan.domain.models import DataMode, ErrorCode, RunState
from qscan.domain.rules import RuleConfig


class WatchlistService:
    def __init__(self, repository: Repository, provider: Provider, lock: FileLock) -> None:
        self.repository, self.provider, self.lock = repository, provider, lock

    def list(self) -> tuple[Watchlist, ...]:
        return self.repository.watchlists()

    def lookup(self, name_or_id: str) -> Watchlist:
        try:
            identity = UUID(name_or_id)
        except ValueError:
            matches = [w for w in self.list() if w.name == name_or_id]
            if len(matches) != 1:
                raise ApplicationError(
                    ErrorCode.NOT_FOUND if not matches else ErrorCode.VALIDATION_ERROR,
                    "Watchlist not found" if not matches else "Ambiguous name; use UUID",
                ) from None
            return matches[0]
        return self.repository.watchlist(identity)

    def import_named(
        self,
        name: str,
        content: bytes,
        format: str = "txt",
        *,
        replace: bool = False,
        expected_revision: int | None = None,
    ) -> Watchlist:
        old = None
        try:
            old = self.lookup(name)
        except ApplicationError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
        if old is not None and not replace:
            raise ApplicationError(
                ErrorCode.WATCHLIST_VERSION_CONFLICT, "Name exists; use --replace explicitly"
            )
        if expected_revision is not None and old is None:
            raise ApplicationError(
                ErrorCode.VALIDATION_ERROR, "Revision requires an existing watchlist"
            )
        return self.import_content(
            old.name if old else name,
            content,
            format,
            watchlist_id=old.id if old else None,
            expected_revision=(expected_revision if expected_revision is not None else old.revision)
            if old
            else None,
        )

    def import_content(
        self,
        name: str,
        content: bytes,
        format: str = "txt",
        *,
        watchlist_id: UUID | None = None,
        expected_revision: int | None = None,
    ) -> Watchlist:
        if len(content) > 1_000_000:
            raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Input exceeds 1 MB")
        try:
            text = content.decode("utf-8-sig")
            rows: list[tuple[str, str | None]]
            if format == "txt":
                rows = [
                    (s.strip(), None)
                    for s in text.splitlines()
                    if s.strip() and not s.lstrip().startswith("#")
                ]
            elif format == "csv":
                reader = csv.DictReader(io.StringIO(text))
                if reader.fieldnames is None or "symbol" not in reader.fieldnames:
                    raise ValueError("CSV requires symbol column")
                rows = []
                for row in reader:
                    symbol = row.get("symbol")
                    if symbol is None or None in row:
                        raise ValueError("Malformed CSV row")
                    if symbol.strip():
                        rows.append((symbol.strip(), row.get("exchange") or None))
            else:
                raise ValueError("Unsupported import format")
            instruments: dict[UUID, Instrument] = {}
            for symbol, exchange in rows:
                if not re.fullmatch(r"[A-Za-z0-9.^=-]{1,32}", symbol):
                    raise ValueError("Invalid symbol")
                instrument = self.provider.resolve(symbol.upper(), exchange)
                if (
                    instrument.id in instruments
                    and instruments[instrument.id].exchange != instrument.exchange
                ):
                    raise ValueError("Conflicting exchange metadata")
                instruments.setdefault(instrument.id, instrument)
                if len(instruments) > 2000:
                    raise ValueError("Watchlist exceeds 2000 symbols")
            with self.lock:
                if watchlist_id is not None:
                    old = self.repository.watchlist(watchlist_id)
                    if old.revision != expected_revision:
                        raise ApplicationError(ErrorCode.WATCHLIST_VERSION_CONFLICT)
                elif expected_revision is not None:
                    raise ValueError("Revision requires a watchlist")
                value = Watchlist(
                    id=watchlist_id or uuid4(),
                    name=name,
                    revision=(expected_revision or 0) + 1,
                    instruments=tuple(instruments.values()),
                )
                self.repository.save_watchlist(value, expected_revision)
            return value
        except (ValueError, UnicodeError, csv.Error) as exc:
            raise ApplicationError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc


class MarketDataService:
    def __init__(
        self,
        repository: Repository,
        provider: Provider,
        clock: Clock,
        lock: FileLock,
        review_interval: timedelta = timedelta(days=30),
    ) -> None:
        if review_interval <= timedelta(0):
            raise ValueError("Review interval must be positive")
        self.repository, self.provider, self.clock, self.lock = repository, provider, clock, lock
        self.review_interval = review_interval

    def refresh(
        self, watchlist_id: UUID, calendar: Calendar, as_of: date | None = None
    ) -> RefreshResult:
        with self.lock:
            watchlist = self.repository.watchlist(watchlist_id)
            context = calendar.resolve(as_of, self.clock.now())
            expected = calendar.sessions(
                context.as_of_session - timedelta(days=1100), context.as_of_session
            )[-504:]
            obtained = tuple(
                self.obtain(i, expected, DataMode.FORCE) for i in watchlist.instruments
            )
            items = tuple(
                RefreshItem(
                    instrument=i.instrument,
                    available=i.series is not None,
                    updated=i.series is not None
                    and i.provenance is not None
                    and not i.provenance.cache,
                    error=i.error,
                    provenance=i.provenance,
                    warnings=(*i.warnings, *(i.provenance.warnings if i.provenance else ())),
                )
                for i in obtained
            )
            successful = sum(i.available for i in items)
            return RefreshResult(
                context=context,
                items=items,
                successful=successful,
                failed=len(items) - successful,
                has_warnings=any(i.warnings for i in items),
            )

    def obtain(
        self, instrument: Instrument, expected: tuple[date, ...], mode: DataMode
    ) -> InputItem:
        with self.lock:
            return self._obtain(instrument, expected, mode)

    def _obtain(
        self, instrument: Instrument, expected: tuple[date, ...], mode: DataMode
    ) -> InputItem:
        if instrument.instrument_type == "UNSUPPORTED":
            return InputItem(instrument=instrument, error=ErrorCode.UNSUPPORTED_INSTRUMENT)
        cached = self.repository.cache(instrument)
        usable: InputItem | None = None
        cache_error = ErrorCode.NO_DATA
        if cached is not None:
            if not cached.trusted or cached.provenance.provider != self.provider.name:
                cache_error = ErrorCode.ADJUSTMENT_REVIEW_REQUIRED
            else:
                try:
                    series, warnings = validate(
                        instrument,
                        RawPrices(
                            tuple(zip(cached.series.sessions, cached.series.closes, strict=True)),
                            provider=cached.provenance.provider,
                        ),
                        expected,
                    )
                    usable = InputItem(
                        instrument=instrument,
                        series=series,
                        warnings=warnings,
                        provenance=cached.provenance.model_copy(update={"cache": True}),
                    )
                except ApplicationError as exc:
                    cache_error = exc.code
        # This return precedes every provider call, including symbol resolution.
        if mode == DataMode.CACHE_ONLY:
            return usable or InputItem(instrument=instrument, error=cache_error)
        now = self.clock.now().astimezone(UTC)
        due = cached is None or now - cached.reviewed_at >= self.review_interval
        needs_older_history = cached is not None and (
            expected[-1] < cached.series.sessions[-1] and expected[0] < cached.series.sessions[0]
        )
        if mode == DataMode.AUTO and usable is not None and not due and not needs_older_history:
            return usable
        revisions = cached.provenance.revisions if cached else ()
        distrust = cached is not None and (
            not cached.trusted or cached.provenance.provider != self.provider.name
        )
        full = mode == DataMode.FORCE or due or distrust or cached is None or needs_older_history
        try:
            if not full and cached is not None:
                start = cached.series.sessions[max(0, len(cached.series.sessions) - 10)]
                if start > expected[-1]:
                    full = True
                else:
                    raw = self._fetch(instrument, start, expected[-1])
                    previous = dict(zip(cached.series.sessions, cached.series.closes, strict=True))
                    changed = (
                        raw.basis != cached.series.price_basis
                        or raw.adjustment_review
                        or bool(raw.split_sessions)
                        or any(
                            s in previous
                            and math.isfinite(c)
                            and not math.isclose(c, previous[s], rel_tol=1e-8, abs_tol=1e-10)
                            for s, c in raw.rows
                            if s <= expected[-1]
                        )
                    )
                    if changed:
                        distrust = True
                        reason = now.isoformat() + ":overlap_revision_or_basis"
                        revisions = (*revisions, reason)
                        self.repository.invalidate_cache(instrument, reason)
                        full = True
                    else:
                        overlap_expected = tuple(s for s in expected if s >= start)
                        validate(instrument, raw, overlap_expected)
                        # Keep returned ordering and duplicates for validation; do not deduplicate.
                        raw = RawPrices(
                            tuple((s, c) for s, c in previous.items() if s < start) + raw.rows,
                            raw.provider,
                            raw.basis,
                        )
            if full:
                raw = self._fetch(instrument, expected[0], expected[-1])
                if raw.adjustment_review or raw.basis != "split_adjusted_close":
                    distrust = True
                    self.repository.invalidate_cache(
                        instrument, now.isoformat() + ":basis_unverified"
                    )
            if cached is not None and full:
                previous = dict(zip(cached.series.sessions, cached.series.closes, strict=True))
                if any(
                    s in previous and not math.isclose(c, previous[s], rel_tol=1e-8, abs_tol=1e-10)
                    for s, c in raw.rows
                    if s <= expected[-1] and math.isfinite(c)
                ):
                    distrust = True
                    reason = now.isoformat() + ":full_history_revision"
                    revisions = (*revisions, reason)
                    self.repository.invalidate_cache(instrument, reason)
            series, warnings = validate(instrument, raw, expected)
            fetched_at = self.clock.now().astimezone(UTC)
            provenance = Provenance(
                provider=raw.provider, fetched_at=fetched_at, revisions=revisions, warnings=warnings
            )
            self.repository.replace_cache(
                instrument,
                CacheEntry(
                    series=series,
                    provenance=provenance,
                    reviewed_at=fetched_at if full or cached is None else cached.reviewed_at,
                ),
            )
            return InputItem(
                instrument=instrument, series=series, warnings=warnings, provenance=provenance
            )
        except ApplicationError as exc:
            if usable is not None and not distrust:
                assert usable.provenance is not None
                return usable.model_copy(
                    update={
                        "provenance": usable.provenance.model_copy(
                            update={
                                "warnings": (
                                    *usable.provenance.warnings,
                                    "refresh_failed:" + exc.code.value,
                                )
                            }
                        )
                    }
                )
            return InputItem(
                instrument=instrument,
                error=(ErrorCode.ADJUSTMENT_REVIEW_REQUIRED if distrust else exc.code),
            )

    def _fetch(self, instrument: Instrument, start: date, end: date) -> RawPrices:
        try:
            return self.provider.fetch(instrument, start, end)
        except ApplicationError:
            raise
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise ApplicationError(ErrorCode.NO_DATA, type(exc).__name__) from exc
        except Exception as exc:
            raise ApplicationError(ErrorCode.INTERNAL_ERROR, type(exc).__name__) from exc


class ScanQueryService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def get(self, identity: UUID) -> Run:
        return self.repository.run(identity)

    def list(self) -> tuple[Run, ...]:
        return self.repository.runs()

    def summaries(self, limit: int = 30) -> tuple[Run, ...]:
        if not 1 <= limit <= 200:
            raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Limit must be 1..200")
        return self.repository.summaries(limit)


class ScanService:
    def __init__(
        self,
        repository: Repository,
        market: MarketDataService,
        calendar: Calendar,
        clock: Clock,
        snapshots: Snapshots,
        lock: FileLock,
    ) -> None:
        self.repository, self.market, self.calendar = repository, market, calendar
        self.clock, self.snapshots, self.lock = clock, snapshots, lock

    def scan(
        self,
        watchlist_id: UUID,
        *,
        as_of: date | None = None,
        rules: RuleConfig | None = None,
        mode: DataMode = DataMode.AUTO,
    ) -> Run:
        with self.lock:
            watchlist = self.repository.watchlist(watchlist_id)
            context = self.calendar.resolve(as_of, self.clock.now())
            expected = self.calendar.sessions(
                context.as_of_session - timedelta(days=1100), context.as_of_session
            )[-504:]
            now = self.clock.now().astimezone(UTC)
            config = rules or RuleConfig()
            run = Run(
                id=uuid4(),
                state=RunState.RUNNING,
                context=context,
                watchlist=watchlist,
                rules=config,
                config_hash=config.config_hash(),
                requested_at=now,
                started_at=now,
                counts=Counts(
                    requested=len(watchlist.instruments), data_error=len(watchlist.instruments)
                ),
            )
            from qscan.application.reporting import ComparisonService

            run = run.model_copy(
                update={"comparison": ComparisonService(self.repository, self.snapshots).bind(run)}
            )
            self.repository.create_run(run)
            started = perf_counter()
            try:
                items = tuple(self.market.obtain(i, expected, mode) for i in watchlist.instruments)
                snapshot = InputSnapshot(
                    context=context,
                    rules=config,
                    watchlist=watchlist,
                    calendar_version=self.calendar.version,
                    expected_sessions=expected,
                    items=items,
                )
                return self._complete(
                    run, snapshot, {"market_validation": perf_counter() - started}
                )
            except Exception:
                self._failed(run)
                raise

    def replay(self, source_id: UUID) -> Run:
        with self.lock:
            source = self.repository.run(source_id)
            if source.input_hash is None:
                raise ApplicationError(ErrorCode.SCAN_NOT_READY)
            snapshot = self.snapshots.read(source.input_hash)
            if (
                snapshot.context != source.context
                or snapshot.rules.config_hash() != source.config_hash
            ):
                raise ApplicationError(ErrorCode.SCAN_FAILED, "Snapshot context mismatch")
            now = self.clock.now().astimezone(UTC)
            run = Run(
                id=uuid4(),
                state=RunState.RUNNING,
                context=snapshot.context,
                watchlist=snapshot.watchlist,
                rules=snapshot.rules,
                config_hash=snapshot.rules.config_hash(),
                source_run_id=source_id,
                requested_at=now,
                started_at=now,
                counts=Counts(requested=len(snapshot.items), data_error=len(snapshot.items)),
            )
            self.repository.create_run(run)
            try:
                return self._complete(run, snapshot, {})
            except Exception:
                self._failed(run)
                raise

    def _failed(self, run: Run) -> None:
        self.repository.fail(
            run.model_copy(
                update={
                    "state": RunState.FAILED,
                    "finished_at": self.clock.now().astimezone(UTC),
                    "error": ErrorCode.SCAN_FAILED,
                }
            )
        )

    def _complete(self, run: Run, snapshot: InputSnapshot, timings: dict[str, float]) -> Run:
        started = perf_counter()
        digest = self.snapshots.write(snapshot)
        timings["snapshot"] = perf_counter() - started
        started = perf_counter()
        analyses = []
        output = []
        evaluated = excluded = data_error = 0
        for item in snapshot.items:
            if item.series is not None:
                analysis = analyze_symbol(item.series, snapshot.rules, snapshot.context)
            else:
                analysis = SymbolAnalysis(
                    instrument_id=item.instrument.id,
                    context=snapshot.context,
                    config_hash=run.config_hash,
                    evaluation_status="DATA_UNAVAILABLE",
                    reasons=(Reason(code=(item.error or ErrorCode.INTERNAL_ERROR).value),),
                )
            if analysis.evaluation_status == "EVALUATED":
                category = "evaluated"
                evaluated += 1
            elif analysis.reasons[0].code in {
                ErrorCode.INSUFFICIENT_HISTORY.value,
                ErrorCode.UNSUPPORTED_INSTRUMENT.value,
            }:
                category = "excluded"
                excluded += 1
            else:
                category = "data_error"
                data_error += 1
            analyses.append(analysis)
            output.append(
                ScanResult.model_validate(
                    dict(
                        instrument=item.instrument,
                        analysis=analysis,
                        category=category,
                        warnings=item.warnings,
                        provenance=item.provenance,
                    )
                )
            )
        ranks = {
            r.analysis.instrument_id: r.rank for r in rank_candidates(analyses, snapshot.rules)
        }
        output = [r.model_copy(update={"rank": ranks.get(r.instrument.id)}) for r in output]
        timings["core"] = perf_counter() - started
        state = (
            RunState.FAILED
            if not evaluated
            else (RunState.PARTIAL if data_error else RunState.SUCCEEDED)
        )
        completed = run.model_copy(
            update={
                "input_hash": digest,
                "state": state,
                "finished_at": self.clock.now().astimezone(UTC),
                "timings": timings,
                "counts": Counts(
                    requested=len(output),
                    evaluated=evaluated,
                    excluded=excluded,
                    data_error=data_error,
                    candidate=len(ranks),
                ),
                "results": tuple(output),
            }
        )
        from qscan.application.reporting import ComparisonService

        completed = completed.model_copy(
            update={
                "comparison": ComparisonService(self.repository, self.snapshots).complete(
                    completed, snapshot
                )
            }
        )
        self.repository.publish(completed)
        return self.repository.run(run.id)
