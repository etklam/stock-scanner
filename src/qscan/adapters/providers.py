"""Offline fixtures and a gated Yahoo adapter; symbol resolution never uses network I/O."""

import time
from datetime import date, timedelta
from threading import BoundedSemaphore
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

import pandas as pd

from qscan.application.contracts import ApplicationError, Instrument, RawPrices
from qscan.domain.models import ErrorCode


def resolve_us(symbol: str, exchange: str | None = None) -> Instrument:
    """Only documented US class shares are aliases; other punctuation stays intact."""
    symbol = symbol.strip().upper()
    provider_symbol = {"BRK.B": "BRK-B", "BRK.A": "BRK-A", "BF.B": "BF-B", "BF.A": "BF-A"}.get(
        symbol, symbol
    )
    market = (exchange or "NYSE").strip().upper()
    supported = (
        market in {"NYSE", "NASDAQ", "AMEX"}
        and not provider_symbol.endswith(("-USD", "-USDT", "-EUR", "-BTC", "-ETH"))
        and all(c.isascii() and (c.isalnum() or c == "-") for c in provider_symbol)
    )
    return Instrument(
        id=uuid5(NAMESPACE_URL, "qscan:US:" + provider_symbol),
        display_symbol=symbol,
        provider_symbol=provider_symbol,
        exchange=market,
        instrument_type="EQUITY" if supported else "UNSUPPORTED",
    )


class FixtureProvider:
    name = "fixture"

    def __init__(self, data: dict[str, RawPrices | ApplicationError]) -> None:
        self.data = data
        self.calls: list[tuple[str, date, date]] = []

    def resolve(self, symbol: str, exchange: str | None = None) -> Instrument:
        return resolve_us(symbol, exchange)

    def fetch(self, instrument: Instrument, start: date, end: date) -> RawPrices:
        self.calls.append((instrument.provider_symbol, start, end))
        value = self.data.get(instrument.provider_symbol)
        if value is None:
            raise ApplicationError(ErrorCode.NO_DATA)
        if isinstance(value, ApplicationError):
            raise value
        return RawPrices(
            rows=tuple((s, c) for s, c in value.rows if start <= s <= end),
            provider=value.provider,
            basis=value.basis,
            adjustment_review=value.adjustment_review,
            split_sessions=tuple(s for s in value.split_sessions if start <= s <= end),
        )


def yahoo_rows(frame: pd.DataFrame, symbol: str) -> RawPrices:
    """Accept flat, field-first, and ticker-first frames, discarding all non-Close signals."""
    if frame.empty:
        raise ApplicationError(ErrorCode.NO_DATA)
    if isinstance(frame.columns, pd.MultiIndex):
        if symbol in frame.columns.get_level_values(1):
            frame = pd.DataFrame(frame.xs(symbol, axis=1, level=1))
        elif symbol in frame.columns.get_level_values(0):
            frame = pd.DataFrame(frame.xs(symbol, axis=1, level=0))
        else:
            raise ApplicationError(ErrorCode.NO_DATA)
    if "Close" not in frame:
        raise ApplicationError(ErrorCode.NO_DATA)
    close = frame["Close"]
    if close.isna().all():
        raise ApplicationError(ErrorCode.NO_DATA)
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Expected datetime index")
    rows = tuple(
        (stamp.date(), float(value)) for stamp, value in zip(frame.index, close, strict=True)
    )
    splits = tuple(
        stamp.date()
        for stamp, value in zip(
            frame.index, frame.get("Stock Splits", pd.Series(0, index=frame.index)), strict=True
        )
        if pd.notna(value) and value != 0
    )
    return RawPrices(rows=rows, provider="yahoo", split_sessions=splits)


class Downloader(Protocol):
    def __call__(
        self, symbol: str, start: date, end_exclusive: date, timeout: float
    ) -> pd.DataFrame: ...


def download(symbol: str, start: date, end_exclusive: date, timeout: float) -> pd.DataFrame:
    import yfinance as yf  # type: ignore[import-untyped]

    frame: pd.DataFrame | None = yf.download(
        symbol,
        start=start.isoformat(),
        end=end_exclusive.isoformat(),
        interval="1d",
        auto_adjust=False,
        back_adjust=False,
        repair=False,
        prepost=False,
        actions=True,
        threads=False,
        progress=False,
        timeout=timeout,
        keepna=True,
        group_by="column",
        multi_level_index=True,
    )
    return frame if frame is not None else pd.DataFrame()


class YahooProvider:
    name = "yahoo"

    def __init__(
        self,
        *,
        diagnostic: bool = False,
        downloader: Downloader = download,
        attempts: int = 2,
        timeout: float = 15,
        concurrency: int = 2,
    ) -> None:
        if not 1 <= attempts <= 3 or not 1 <= concurrency <= 2 or not 0 < timeout <= 60:
            raise ValueError("Invalid bounded provider settings")
        self.diagnostic = diagnostic
        self.downloader = downloader
        self.attempts = attempts
        self.timeout = timeout
        self.limit = BoundedSemaphore(concurrency)

    def resolve(self, symbol: str, exchange: str | None = None) -> Instrument:
        return resolve_us(symbol, exchange)

    def fetch(self, instrument: Instrument, start: date, end: date) -> RawPrices:
        # This switch enables diagnostics, not acceptance of the unverified price basis.
        if not self.diagnostic:
            raise ApplicationError(ErrorCode.ADJUSTMENT_REVIEW_REQUIRED, "Yahoo release is BLOCKED")
        if instrument.instrument_type == "UNSUPPORTED":
            raise ApplicationError(ErrorCode.UNSUPPORTED_INSTRUMENT)
        with self.limit:
            for attempt in range(self.attempts):
                try:
                    raw = yahoo_rows(
                        self.downloader(
                            instrument.provider_symbol, start, end + timedelta(days=1), self.timeout
                        ),
                        instrument.provider_symbol,
                    )
                    # Market identity and Close basis still require live/manual acceptance.
                    return RawPrices(raw.rows, raw.provider, raw.basis, True, raw.split_sessions)
                except Exception as exc:
                    if attempt + 1 == self.attempts:
                        if isinstance(exc, ApplicationError):
                            raise
                        raise ApplicationError(ErrorCode.NO_DATA, type(exc).__name__) from exc
                    time.sleep(0.25 * 2**attempt)
        raise AssertionError("Unreachable")
