"""Implemented API contracts: request models, light status DTOs, result detail DTOs."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue

from qscan.domain.analysis import Reason, SymbolAnalysis, WindowAnalysis
from qscan.domain.models import Contract, DataMode, ErrorCode

Symbol = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.^=-]+$")]


class CreateWatchlist(Contract):
    name: str = Field(min_length=1, max_length=100)
    symbols: tuple[Symbol, ...] = Field(min_length=1, max_length=2000)


class PatchWatchlist(Contract):
    name: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class ReplaceSymbols(Contract):
    expected_revision: int = Field(ge=1)
    symbols: tuple[Symbol, ...] = Field(min_length=1, max_length=2000)


class CreateScan(Contract):
    watchlist_id: UUID
    ruleset_id: Literal["breakout-v1"] = "breakout-v1"
    as_of_session: date | None = None
    data_mode: DataMode = DataMode.AUTO


class ErrorBody(Contract):
    code: ErrorCode
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: UUID


class ErrorEnvelope(Contract):
    error: ErrorBody


# --- Response DTOs; polling endpoints stay lightweight by design. ---


class WatchlistOut(Contract):
    id: UUID
    name: str
    revision: int
    symbols: tuple[str, ...]
    links: "WatchlistLinks"


class WatchlistLinks(Contract):
    self: str
    symbols: str


class RulesetOut(Contract):
    id: str
    version: str
    windows: tuple[int, ...]
    minimum_history: int
    description: str


class ProgressOut(Contract):
    phase: Literal["market_data", "analysis", "publication"]
    processed_symbols: int
    total_symbols: int
    updated_at: datetime


class CountsOut(Contract):
    requested: int
    evaluated: int
    excluded: int
    data_error: int
    candidate: int


class ScanLinks(Contract):
    self: str
    results: str
    changes: str
    export: str
    watchlist: str


class ScanStatusOut(Contract):
    """Light polling document: identity, state, sessions, progress, coverage, links."""

    id: UUID
    state: str
    as_of_session: date
    reference_session: date
    watchlist_id: UUID
    watchlist_revision: int
    ruleset_id: str
    ruleset_version: str
    engine_version: str
    data_mode: str
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: ProgressOut | None = None
    counts: CountsOut | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()
    source_run_id: UUID | None = None
    links: ScanLinks


class InstrumentOut(Contract):
    id: UUID
    display_symbol: str
    exchange: str
    currency: str
    instrument_type: str


class ResultOut(Contract):
    """Full per-symbol detail; never included in polling/status payloads."""

    instrument: InstrumentOut
    analysis: SymbolAnalysis
    reasons: tuple[Reason, ...]
    warnings: tuple[str, ...]
    category: Literal["evaluated", "excluded", "data_error"]
    rank: int | None
    alternative_windows: tuple[WindowAnalysis, ...]


class Page(Contract):
    next_cursor: str | None = None


class ResultsPage(Page):
    items: tuple[ResultOut, ...]


class ScansPage(Page):
    items: tuple[ScanStatusOut, ...]


class SeriesOut(Contract):
    run_id: UUID
    instrument_id: UUID
    symbol: str
    sessions: tuple[date, ...]
    closes: tuple[float, ...]
    sma: dict[str, tuple[float | None, ...]]
    window_start: date | None = None
    window_end: date | None = None
    close_resistance: float | None = None
    displayed_sessions: int
    price_basis: str
    as_of_session: date


class SessionOut(Contract):
    """Latest completed market session new scans would use."""

    as_of_session: date
    reference_session: date


class ScanAccepted(Contract):
    id: UUID
    state: Literal["QUEUED"] = "QUEUED"
    as_of_session: date
    watchlist_revision: int = Field(ge=1)
    ruleset_version: str
    links: ScanLinks


# --- human review labels (mutable data; never part of scan results) ---


class PutReview(Contract):
    label: Literal["worth_reviewing", "borderline", "not_useful"]
    note: str = Field(default="", max_length=500)
    # Required when a label already exists so concurrent editors cannot
    # silently overwrite each other; omit it only for a first-time label.
    expected_revision: int | None = Field(default=None, ge=1)


class SavedReviewOut(Contract):
    run_id: UUID
    instrument_id: UUID
    label: str
    note: str
    revision: int
    updated_at: datetime


class ReviewsPage(Contract):
    items: tuple[SavedReviewOut, ...]
