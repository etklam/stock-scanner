"""Immutable close-only inputs and stable machine-readable states."""

from datetime import date
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PriceBasis(StrEnum):
    SPLIT_ADJUSTED_CLOSE = "split_adjusted_close"


class DataMode(StrEnum):
    AUTO = "auto"
    CACHE_ONLY = "cache_only"
    FORCE = "force"


class Stage(StrEnum):
    FORMING = "FORMING"
    NEAR_CLOSE_RESISTANCE = "NEAR_CLOSE_RESISTANCE"
    CLOSE_BREAK_ABOVE = "CLOSE_BREAK_ABOVE"
    EXTENDED = "EXTENDED"


class RunState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ErrorCode(StrEnum):
    WORKER_INTERRUPTED = "WORKER_INTERRUPTED"
    NO_DATA = "NO_DATA"
    STALE_DATA = "STALE_DATA"
    MISSING_REQUIRED_SESSION = "MISSING_REQUIRED_SESSION"
    INVALID_CLOSE = "INVALID_CLOSE"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"
    SUSPICIOUS_FLAT_SERIES = "SUSPICIOUS_FLAT_SERIES"
    ADJUSTMENT_REVIEW_REQUIRED = "ADJUSTMENT_REVIEW_REQUIRED"
    INVALID_AS_OF_SESSION = "INVALID_AS_OF_SESSION"
    SESSION_NOT_COMPLETE = "SESSION_NOT_COMPLETE"
    WATCHLIST_VERSION_CONFLICT = "WATCHLIST_VERSION_CONFLICT"
    WATCHLIST_IN_USE = "WATCHLIST_IN_USE"
    SCAN_NOT_READY = "SCAN_NOT_READY"
    SCAN_FAILED = "SCAN_FAILED"
    EXECUTION_INCOMPATIBLE = "EXECUTION_INCOMPATIBLE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    REVIEW_REVISION_CONFLICT = "REVIEW_REVISION_CONFLICT"
    QUEUE_LIMIT_REACHED = "QUEUE_LIMIT_REACHED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    REPORT_ERROR = "REPORT_ERROR"


class CloseSeries(Contract):
    """Validated prices; session continuity is checked against the market calendar upstream."""

    instrument_id: UUID
    sessions: tuple[date, ...]
    closes: tuple[FiniteFloat, ...]
    price_basis: PriceBasis = PriceBasis.SPLIT_ADJUSTED_CLOSE

    @model_validator(mode="after")
    def validate_series(self) -> Self:
        if not self.sessions or len(self.sessions) != len(self.closes):
            raise ValueError("Sessions and closes must be nonempty and equal in length")
        if any(a >= b for a, b in zip(self.sessions, self.sessions[1:], strict=False)):
            raise ValueError("Sessions must be unique and strictly increasing")
        if any(close <= 0 for close in self.closes):
            raise ValueError("Closes must be positive")
        return self


class ScanContext(Contract):
    as_of_session: date
    reference_session: date
    engine_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.reference_session >= self.as_of_session:
            raise ValueError("Reference session must precede as-of session")
        return self
