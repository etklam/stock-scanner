"""Validate close-only data without filling or compressing missing sessions."""

import math
from datetime import date

from qscan.application.contracts import ApplicationError, Instrument, RawPrices
from qscan.domain.models import CloseSeries, ErrorCode


def validate(
    instrument: Instrument,
    raw: RawPrices,
    expected: tuple[date, ...],
) -> tuple[CloseSeries, tuple[str, ...]]:
    if instrument.instrument_type == "UNSUPPORTED":
        raise ApplicationError(ErrorCode.UNSUPPORTED_INSTRUMENT)
    if raw.adjustment_review or raw.basis != "split_adjusted_close":
        raise ApplicationError(ErrorCode.ADJUSTMENT_REVIEW_REQUIRED)
    rows = [(s, c) for s, c in raw.rows if expected[0] <= s <= expected[-1]]
    if not rows:
        raise ApplicationError(ErrorCode.NO_DATA)
    values: dict[date, float] = {}
    previous: date | None = None
    for session, close in rows:
        if not math.isfinite(close) or close <= 0:
            raise ApplicationError(ErrorCode.INVALID_CLOSE)
        if session in values and values[session] != close:
            raise ApplicationError(ErrorCode.CONFLICTING_DUPLICATE)
        if previous is not None and session < previous:
            raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Unsorted sessions")
        if session not in expected:
            raise ApplicationError(ErrorCode.MISSING_REQUIRED_SESSION, "Non-session row")
        values[session] = close
        previous = session
    if expected[-1] not in values:
        raise ApplicationError(ErrorCode.STALE_DATA)
    # Only the contiguous suffix can enter the core. Older gaps cannot become adjacency.
    start = len(expected) - 1
    while start > 0 and expected[start - 1] in values:
        start -= 1
    suffix = expected[start:]
    gap = any(s < suffix[0] for s in values)
    if gap and len(suffix) < 80:
        raise ApplicationError(ErrorCode.MISSING_REQUIRED_SESSION)
    closes = tuple(values[s] for s in suffix)
    if any(max(a, b) / min(a, b) >= 3 for a, b in zip(closes, closes[1:], strict=False)):
        raise ApplicationError(ErrorCode.ADJUSTMENT_REVIEW_REQUIRED)
    return CloseSeries(instrument_id=instrument.id, sessions=suffix, closes=closes), (
        (ErrorCode.MISSING_REQUIRED_SESSION.value,) if gap else ()
    )
