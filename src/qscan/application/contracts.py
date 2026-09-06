"""Small typed boundaries and versioned persisted contracts."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from qscan.domain.analysis import SymbolAnalysis
from qscan.domain.models import CloseSeries, Contract, DataMode, ErrorCode, RunState, ScanContext
from qscan.domain.rules import RuleConfig


class NullLock:
    """No-op stand-in for the executor FileLock.

    serve holds the real data-directory ownership lock for its whole lifetime;
    services must not re-acquire that file from other threads (filelock
    instances are not re-entrant across instances/threads).
    """

    timeout = 0

    def acquire(self, timeout: float | None = None, poll_interval: float = 0.05) -> bool:
        return True

    def release(self) -> None: ...

    def __enter__(self) -> "NullLock":
        return self

    def __exit__(self, *exc: object) -> None: ...


class ServiceLock(Protocol):
    """Context-manager surface of FileLock/NullLock; services never call acquire."""

    def __enter__(self) -> Any: ...

    def __exit__(self, exc_type: Any = None, exc_val: Any = None, exc_tb: Any = None) -> None: ...


class ApplicationError(Exception):
    def __init__(
        self, code: ErrorCode, message: str = "", details: dict[str, object] | None = None
    ) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message or code.value)


@dataclass(frozen=True)
class ApplicationContext:
    """Trusted bootstrap identity, never constructed from a request owner field."""

    principal: str = "local"


class Instrument(Contract):
    id: UUID
    display_symbol: str
    provider_symbol: str
    exchange: str = "NYSE"
    currency: str = "USD"
    instrument_type: Literal["EQUITY", "ETF", "UNSUPPORTED", "UNVERIFIED"] = "EQUITY"


class Watchlist(Contract):
    id: UUID
    name: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    instruments: tuple[Instrument, ...] = Field(min_length=1, max_length=2000)


@dataclass(frozen=True)
class RawPrices:
    rows: tuple[tuple[date, float], ...]
    provider: str = "fixture"
    basis: str = "split_adjusted_close"
    adjustment_review: bool = False
    split_sessions: tuple[date, ...] = ()
    instrument: Instrument | None = None


class Provenance(Contract):
    provider: str
    fetched_at: datetime
    cache: bool = False
    warnings: tuple[str, ...] = ()
    revisions: tuple[str, ...] = ()


class CacheEntry(Contract):
    instrument: Instrument | None = None
    series: CloseSeries
    provenance: Provenance
    reviewed_at: datetime
    trusted: bool = True


class InputItem(Contract):
    instrument: Instrument
    series: CloseSeries | None = None
    error: ErrorCode | None = None
    warnings: tuple[str, ...] = ()
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def valid_input(self) -> "InputItem":
        if (self.series is None) == (self.error is None):
            raise ValueError("Input must contain exactly one series or error")
        if self.series is not None and self.series.instrument_id != self.instrument.id:
            raise ValueError("Input identity mismatch")
        return self


class RefreshItem(Contract):
    instrument: Instrument
    available: bool
    updated: bool
    error: ErrorCode | None = None
    warnings: tuple[str, ...] = ()
    provenance: Provenance | None = None


class RefreshResult(Contract):
    context: ScanContext
    items: tuple[RefreshItem, ...]
    successful: int
    failed: int
    has_warnings: bool


class InputSnapshot(Contract):
    schema_version: Literal[1] = 1
    context: ScanContext
    rules: RuleConfig
    watchlist: Watchlist
    calendar_version: str
    expected_sessions: tuple[date, ...]
    items: tuple[InputItem, ...]

    @model_validator(mode="after")
    def valid_snapshot(self) -> "InputSnapshot":
        if tuple(i.instrument for i in self.items) != self.watchlist.instruments:
            raise ValueError("Snapshot members differ from watchlist")
        if len({i.instrument.id for i in self.items}) != len(self.items):
            raise ValueError("Duplicate snapshot instrument")
        expected = self.expected_sessions
        if len(expected) < 2 or tuple(sorted(set(expected))) != expected:
            raise ValueError("Invalid validation sessions")
        if expected[-2:] != (self.context.reference_session, self.context.as_of_session):
            raise ValueError("Validation context mismatch")
        for item in self.items:
            if (
                item.series is not None
                and item.series.sessions != expected[-len(item.series.sessions) :]
            ):
                raise ValueError("Series must be a contiguous suffix of validation sessions")
        return self


class Counts(Contract):
    requested: int = Field(ge=0)
    evaluated: int = Field(default=0, ge=0)
    excluded: int = Field(default=0, ge=0)
    data_error: int = Field(default=0, ge=0)
    candidate: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> "Counts":
        pending = self.evaluated == self.excluded == self.data_error == 0
        if not pending and self.requested != self.evaluated + self.excluded + self.data_error:
            # Pending (QUEUED/not-yet-claimed) runs claim nothing; terminal states must add up.
            raise ValueError("Inconsistent counts")
        if self.candidate > self.evaluated:
            raise ValueError("Candidates exceed evaluations")
        return self


class Progress(Contract):
    """Live worker progress; coverage counts stay separate and unset until publication."""

    phase: Literal["market_data", "analysis", "publication"]
    processed_symbols: int = Field(ge=0)
    total_symbols: int = Field(ge=1)
    updated_at: datetime


class ScanResult(Contract):
    instrument: Instrument
    analysis: SymbolAnalysis
    category: Literal["evaluated", "excluded", "data_error"]
    rank: int | None = None
    warnings: tuple[str, ...] = ()
    provenance: Provenance | None = None


ChangeCode = Literal[
    "NEW_CANDIDATE",
    "DROPPED_CANDIDATE",
    "STAGE_CHANGED",
    "SCORE_CHANGED",
    "WINDOW_CHANGED",
    "UNIVERSE_CHANGED",
    "COMPARISON_UNAVAILABLE",
    "DATA_REVISION_DIFF",
    "NO_BASELINE",
]


class Change(Contract):
    instrument_id: UUID | None = None
    symbol: str | None = None
    codes: tuple[ChangeCode, ...]
    reasons: tuple[str, ...] = ()
    previous: SymbolAnalysis | None = None
    current: SymbolAnalysis | None = None


class Comparison(Contract):
    semantic_version: Literal[1] = 1
    previous_run_id: UUID | None = None
    previous_session: date
    current_session: date
    binding: Literal["recorded", "legacy_unavailable", "replay_unavailable"] = "recorded"
    reasons: tuple[str, ...] = ()
    changes: tuple[Change, ...] = ()


ReviewLabel = Literal["worth_reviewing", "borderline", "not_useful"]


class SavedReview(Contract):
    """One human review label scoped to (owner, run, instrument).

    Mutable review data kept strictly separate from immutable scan results:
    saving a label never touches scores, ranks, hashes or snapshots, never
    carries across runs automatically, and an exact replay starts unlabeled.
    """

    run_id: UUID
    instrument_id: UUID
    label: ReviewLabel
    note: str = Field(default="", max_length=500)
    revision: int = Field(ge=1)
    updated_at: datetime


class Run(Contract):
    id: UUID
    state: RunState
    context: ScanContext
    watchlist: Watchlist
    rules: RuleConfig
    config_hash: str
    input_hash: str | None = None
    comparison: Comparison | None = None
    price_basis: str = "split_adjusted_close"
    source_run_id: UUID | None = None
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: Counts
    progress: Progress | None = None
    data_mode: DataMode = DataMode.AUTO
    # Provider this run was accepted under; execution refuses a mismatched server.
    provider: str | None = None
    timings: dict[str, float] = {}
    error: ErrorCode | None = None
    warnings: tuple[str, ...] = ()
    results: tuple[ScanResult, ...] = ()


class Clock(Protocol):
    def now(self) -> datetime: ...


class Calendar(Protocol):
    version: str

    def sessions(self, start: date, end: date) -> tuple[date, ...]: ...
    def resolve(self, requested: date | None, now: datetime) -> ScanContext: ...


class Provider(Protocol):
    name: str

    def resolve(self, symbol: str, exchange: str | None = None) -> Instrument: ...
    def fetch(self, instrument: Instrument, start: date, end: date) -> RawPrices: ...


class Repository(Protocol):
    provider: str

    def save_watchlist(self, value: Watchlist, expected_revision: int | None) -> None: ...
    def delete_watchlist(self, identity: UUID) -> None: ...
    def watchlist(self, identity: UUID) -> Watchlist: ...
    def watchlists(self) -> tuple[Watchlist, ...]: ...
    def cache(self, instrument: Instrument) -> CacheEntry | None: ...
    def replace_cache(self, instrument: Instrument, value: CacheEntry) -> None: ...
    def invalidate_cache(self, instrument: Instrument, reason: str) -> None: ...
    def create_run(self, run: Run) -> None: ...
    def enqueue(
        self, run: Run, idempotency_key: str, request_hash: str, queue_limit: int
    ) -> tuple[bool, tuple[UUID, str] | None]: ...
    def claim(self, run: Run) -> bool: ...
    def update_progress(self, identity: UUID, progress: Progress) -> None: ...
    def next_queued(self) -> UUID | None: ...
    def run_owner(self, identity: UUID) -> str | None: ...
    def run_by_idempotency(self, key: str) -> tuple[UUID, str] | None: ...
    def recover_interrupted(self) -> int: ...
    def fail_queued(
        self, identity: UUID, error: ErrorCode, warnings: tuple[str, ...] = ()
    ) -> bool: ...
    def runs_page(
        self, *, after: tuple[str, str] | None, limit: int, state: str | None
    ) -> tuple[tuple[Run, ...], bool]: ...
    def run_summary(self, identity: UUID) -> Run: ...
    def results_page(
        self,
        identity: UUID,
        *,
        after: tuple[int, int | None, str] | None,
        limit: int,
        stage: str | None,
        candidate: bool | None,
    ) -> tuple[tuple[ScanResult, ...], bool]: ...
    def publish(self, run: Run) -> None: ...
    def fail(self, run: Run) -> None: ...
    def run(self, identity: UUID) -> Run: ...
    def runs(self) -> tuple[Run, ...]: ...
    def summaries(self, limit: int | None = 30) -> tuple[Run, ...]: ...
    def reviews_for_run(self, identity: UUID) -> tuple[SavedReview, ...]: ...
    def save_review(
        self,
        identity: UUID,
        instrument_id: UUID,
        label: str,
        note: str,
        expected_revision: int | None,
    ) -> SavedReview: ...


class Snapshots(Protocol):
    def write(self, value: InputSnapshot) -> str: ...
    def read(self, digest: str) -> InputSnapshot: ...
