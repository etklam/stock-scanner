from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Lock
from time import sleep

import pandas as pd
import pytest

from qscan.adapters.calendar import NYSECalendar
from qscan.adapters.providers import YahooProvider, download, resolve_us, yahoo_rows
from qscan.application.contracts import ApplicationError, RawPrices
from qscan.application.quality import validate
from qscan.core import analyze_symbol
from qscan.domain.models import ErrorCode
from qscan.domain.rules import RuleConfig


@pytest.fixture(scope="module")
def calendar():
    return NYSECalendar()


@pytest.mark.parametrize(
    "target,instant,code",
    [
        (date(2026, 7, 3), datetime(2026, 7, 4, tzinfo=UTC), ErrorCode.INVALID_AS_OF_SESSION),
        (date(2026, 9, 5), datetime(2026, 9, 6, tzinfo=UTC), ErrorCode.INVALID_AS_OF_SESSION),
        (
            date(2026, 9, 4),
            datetime(2026, 9, 4, 20, 29, tzinfo=UTC),
            ErrorCode.SESSION_NOT_COMPLETE,
        ),
    ],
)
def test_illegal_or_incomplete_explicit_date(calendar, target, instant, code):
    with pytest.raises(ApplicationError) as exc:
        calendar.resolve(target, instant)
    assert exc.value.code == code


def test_calendar_dst_early_close_default_and_override(calendar):
    schedule = calendar.schedule(date(2026, 3, 6), date(2026, 3, 9))
    assert schedule[date(2026, 3, 6)].hour == 21
    assert schedule[date(2026, 3, 9)].hour == 20
    assert calendar.schedule(date(2026, 11, 27), date(2026, 11, 27))[date(2026, 11, 27)].hour == 18
    before = calendar.resolve(None, datetime(2026, 11, 27, 18, 29, tzinfo=UTC))
    after = calendar.resolve(None, datetime(2026, 11, 27, 18, 30, tzinfo=UTC))
    assert before.as_of_session == date(2026, 11, 25)
    assert after.as_of_session == date(2026, 11, 27)
    assert after.reference_session == date(2026, 11, 25)
    override = NYSECalendar(
        overrides={date(2026, 9, 4): None, date(2026, 9, 3): datetime(2026, 9, 3, 17, tzinfo=UTC)}
    )
    assert override.resolve(None, datetime(2026, 9, 4, 23, tzinfo=UTC)).as_of_session == date(
        2026, 9, 3
    )
    assert override.resolve(None, datetime(2026, 9, 3, 17, 30, tzinfo=UTC)).as_of_session == date(
        2026, 9, 3
    )
    with pytest.raises(ApplicationError):
        override.resolve(date(2026, 9, 4), datetime(2026, 9, 5, tzinfo=UTC))


@pytest.fixture(scope="module")
def sample(calendar):
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-129:]
    closes = [50 + i * 0.5 for i in range(88)] + [99, 100] * 20 + [100]
    return sessions, tuple(zip(sessions, closes, strict=True))


@pytest.mark.parametrize(
    "case,code",
    [
        ("empty", ErrorCode.NO_DATA),
        ("stale", ErrorCode.STALE_DATA),
        ("gap", ErrorCode.MISSING_REQUIRED_SESSION),
        ("nan", ErrorCode.INVALID_CLOSE),
        ("inf", ErrorCode.INVALID_CLOSE),
        ("zero", ErrorCode.INVALID_CLOSE),
        ("negative", ErrorCode.INVALID_CLOSE),
        ("duplicate", ErrorCode.CONFLICTING_DUPLICATE),
        ("order", ErrorCode.VALIDATION_ERROR),
        ("basis", ErrorCode.ADJUSTMENT_REVIEW_REQUIRED),
        ("jump", ErrorCode.ADJUSTMENT_REVIEW_REQUIRED),
    ],
)
def test_quality_errors(sample, case, code):
    expected, rows = sample
    basis = "split_adjusted_close"
    if case == "empty":
        rows = ()
    elif case == "stale":
        rows = rows[:-1]
    elif case == "gap":
        rows = rows[:-3] + rows[-2:]
    elif case in {"nan", "inf", "zero", "negative", "jump"}:
        price = {"nan": float("nan"), "inf": float("inf"), "zero": 0, "negative": -1, "jump": 1000}[
            case
        ]
        rows = (*rows[:-1], (rows[-1][0], price))
    elif case == "duplicate":
        rows = (*rows, (rows[-1][0], 101))
    elif case == "order":
        rows = rows[::-1]
    elif case == "basis":
        basis = "adjusted_close"
    with pytest.raises(ApplicationError) as exc:
        validate(resolve_us("GOOD"), RawPrices(rows, basis=basis), expected)
    assert exc.value.code == code


def test_old_gap_truncates_long_windows_and_equal_duplicate(sample, calendar):
    expected, rows = sample
    rows = rows[:35] + rows[36:]
    series, warnings = validate(resolve_us("GOOD"), RawPrices((*rows, rows[-1])), expected)
    assert len(series.closes) == 93
    assert warnings == (ErrorCode.MISSING_REQUIRED_SESSION.value,)
    context = calendar.resolve(expected[-1], datetime(2026, 9, 5, tzinfo=UTC))
    analysis = analyze_symbol(series, RuleConfig(), context)
    assert analysis.features["return_126"] is None
    assert any(w.window_sessions == 40 and not w.available for w in analysis.alternative_windows)


@pytest.mark.parametrize("shape", ["flat", "field_first", "ticker_first"])
def test_ohlcv_and_future_invariance(sample, calendar, shape):
    expected, rows = sample
    frame = pd.DataFrame(
        {
            "Close": [c for _, c in rows],
            "Open": 5.0,
            "High": 7.0,
            "Low": 1.0,
            "Volume": 200,
            "Adj Close": 0.5,
        },
        index=pd.DatetimeIndex(expected),
    )
    if shape != "flat":
        frame = pd.concat({"GOOD": frame, "OTHER": frame * 3}, axis=1)
        if shape == "field_first":
            frame = frame.swaplevel(axis=1)

    def analyze(frame):
        raw = yahoo_rows(frame, "GOOD")
        series, _ = validate(resolve_us("GOOD"), raw, expected)
        return analyze_symbol(
            series, RuleConfig(), calendar.resolve(expected[-1], datetime(2026, 9, 5, tzinfo=UTC))
        )

    first = analyze(frame)
    changed = frame.copy()
    for column in changed.columns:
        if column != "Close" and not (isinstance(column, tuple) and "Close" in column):
            changed[column] = 99999999
    changed.loc[pd.Timestamp(expected[-1] + timedelta(days=4))] = float("nan")
    assert analyze(changed) == first
    assert first.score == 86


def test_mapping_is_explicit_and_not_global():
    assert resolve_us("brk.b").id == resolve_us("BRK-B").id
    assert resolve_us("BF.B").provider_symbol == "BF-B"
    assert resolve_us("XYZ.B").provider_symbol == "XYZ.B"
    assert resolve_us("XYZ.B").instrument_type == "UNSUPPORTED"
    assert resolve_us("AAPL", "LSE").instrument_type == "UNSUPPORTED"
    assert resolve_us("BTC-USD").instrument_type == "UNSUPPORTED"


def test_yahoo_explicit_parameters(monkeypatch):
    import yfinance

    seen = {}

    def fake(symbol, **options):
        seen.update(options)
        return pd.DataFrame()

    monkeypatch.setattr(yfinance, "download", fake)
    download("GOOD", date(2026, 9, 3), date(2026, 9, 5), 12)
    assert {
        k: seen[k] for k in ("interval", "auto_adjust", "back_adjust", "repair", "prepost")
    } == {
        "interval": "1d",
        "auto_adjust": False,
        "back_adjust": False,
        "repair": False,
        "prepost": False,
    }
    assert seen["end"] == "2026-09-05" and seen["timeout"] == 12 and seen["threads"] is False


def test_yahoo_gate_retries_timeout_and_exclusive_end(monkeypatch):
    monkeypatch.setattr("qscan.adapters.providers.time.sleep", lambda _: None)
    calls = []

    def fake(symbol, start, end_exclusive, timeout):
        calls.append((symbol, start, end_exclusive, timeout))
        if len(calls) == 1:
            raise TimeoutError()
        return pd.DataFrame({"Close": [100]}, index=pd.DatetimeIndex(["2026-09-04"]))

    from qscan.adapters.provider_release import ProviderRelease

    monkeypatch.setattr(
        "qscan.adapters.providers.yahoo_release",
        lambda: ProviderRelease(status="BLOCKED", normal_fetch=False, blockers=("test",)),
    )
    provider = YahooProvider(downloader=fake)
    instrument = provider.resolve("GOOD")
    with pytest.raises(ApplicationError) as exc:
        provider.fetch(instrument, date(2026, 9, 3), date(2026, 9, 4))
    assert exc.value.code == ErrorCode.ADJUSTMENT_REVIEW_REQUIRED and not calls
    provider = YahooProvider(diagnostic=True, downloader=fake, timeout=9)
    raw = provider.fetch(instrument, date(2026, 9, 3), date(2026, 9, 4))
    assert len(calls) == 2 and calls[-1][2:] == (date(2026, 9, 5), 9)
    assert raw.adjustment_review
    calls.clear()

    def empty(*args):
        calls.append(args)
        return pd.DataFrame()

    with pytest.raises(ApplicationError):
        YahooProvider(diagnostic=True, downloader=empty).fetch(
            instrument, date(2026, 9, 3), date(2026, 9, 4)
        )
    assert len(calls) == 2


def test_yahoo_bounded_concurrency():
    active = peak = 0
    guard = Lock()

    def fake(*args):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        sleep(0.02)
        with guard:
            active -= 1
        return pd.DataFrame({"Close": [100]}, index=pd.DatetimeIndex(["2026-09-04"]))

    provider = YahooProvider(diagnostic=True, downloader=fake)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(provider.fetch, resolve_us("GOOD"), date(2026, 9, 3), date(2026, 9, 4))
            for _ in range(5)
        ]
        assert all(f.result().rows for f in futures)
    assert peak == 2


def test_multi_ticker_partial_failure():
    frame = pd.DataFrame(
        {("Close", "GOOD"): [100.0], ("Close", "BAD"): [float("nan")]},
        index=pd.DatetimeIndex(["2026-09-04"]),
    )
    assert yahoo_rows(frame, "GOOD").rows == ((date(2026, 9, 4), 100.0),)
    with pytest.raises(ApplicationError) as exc:
        yahoo_rows(frame, "BAD")
    assert exc.value.code == ErrorCode.NO_DATA


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "WRONG"),
        ("currency", "CAD"),
        ("exchangeName", "LSE"),
        ("exchangeTimezoneName", "Europe/London"),
        ("instrumentType", "CRYPTOCURRENCY"),
        ("instrumentType", None),
    ],
)
def test_yahoo_rejects_unknown_market_before_prices(field, value, monkeypatch):
    monkeypatch.setattr("qscan.adapters.providers.time.sleep", lambda _: None)
    metadata = {
        "symbol": "AAPL",
        "exchangeName": "NMS",
        "currency": "USD",
        "exchangeTimezoneName": "America/New_York",
        "instrumentType": "EQUITY",
    }
    metadata[field] = value

    def forbidden(*args):
        raise AssertionError("Unverified market must not download prices")

    provider = YahooProvider(metadata_loader=lambda *args: metadata, downloader=forbidden)
    with pytest.raises(ApplicationError) as error:
        provider.fetch(provider.resolve("AAPL"), date(2026, 9, 3), date(2026, 9, 4))
    assert error.value.code == ErrorCode.UNSUPPORTED_INSTRUMENT


def test_yahoo_etf_hint_and_legacy_identity():
    from qscan.adapters.providers import verified_instrument

    metadata = {
        "symbol": "SPY",
        "exchangeName": "PCX",
        "currency": "USD",
        "exchangeTimezoneName": "America/New_York",
        "instrumentType": "ETF",
    }
    provider = YahooProvider()
    instrument = verified_instrument(provider.resolve("SPY"), metadata)
    assert instrument.instrument_type == "ETF" and instrument.exchange == "NYSE"
    assert instrument.id == resolve_us("SPY").id
    with pytest.raises(ApplicationError, match="contradicts"):
        verified_instrument(provider.resolve("SPY", "NASDAQ"), metadata)
    assert verified_instrument(resolve_us("SPY"), metadata).instrument_type == "ETF"


def test_unaccepted_dependency_blocks_normal_provider(monkeypatch):
    from qscan.adapters.provider_release import yahoo_release

    monkeypatch.setattr("qscan.adapters.provider_release.version", lambda _: "unreviewed")
    status = yahoo_release()
    assert not status.normal_fetch and status.status == "BLOCKED"
    assert len(status.blockers) == 3


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("HTTP 429")])
def test_normal_metadata_failure_is_bounded(error, monkeypatch):
    monkeypatch.setattr("qscan.adapters.providers.time.sleep", lambda _: None)
    calls = []

    def metadata(symbol, timeout):
        calls.append((symbol, timeout))
        raise error

    provider = YahooProvider(metadata_loader=metadata, timeout=7)
    with pytest.raises(ApplicationError) as caught:
        provider.fetch(provider.resolve("AAPL"), date(2026, 9, 3), date(2026, 9, 4))
    assert calls == [("AAPL", 7), ("AAPL", 7)]
    assert caught.value.code == ErrorCode.NO_DATA
