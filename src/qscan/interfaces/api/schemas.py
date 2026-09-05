"""Initial API schemas; handlers, authorization, and job persistence arrive in Phase 4."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue

from qscan.domain.models import Contract, DataMode, ErrorCode

Symbol = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.^=-]+$")]


class CreateWatchlist(Contract):
    name: str = Field(min_length=1, max_length=100)
    symbols: tuple[Symbol, ...] = Field(min_length=1, max_length=2000)


class ReplaceSymbols(Contract):
    expected_revision: int = Field(ge=1)
    symbols: tuple[Symbol, ...] = Field(min_length=1, max_length=2000)


class CreateScan(Contract):
    watchlist_id: UUID
    ruleset_id: Literal["breakout-v1"] = "breakout-v1"
    as_of_session: date | None = None
    data_mode: DataMode = DataMode.AUTO


class ScanLinks(Contract):
    self: str
    results: str


class ScanAccepted(Contract):
    id: UUID
    state: Literal["QUEUED"] = "QUEUED"
    as_of_session: date
    watchlist_revision: int = Field(ge=1)
    ruleset_version: str
    links: ScanLinks


class ErrorBody(Contract):
    code: ErrorCode
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: UUID


class ErrorEnvelope(Contract):
    error: ErrorBody
