"""Dated, bounded live evidence; saves observations, never bulk price history."""

import argparse
import json
import math
import platform
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path

import yfinance as yf
from provider_spike import OPTIONS

from qscan.adapters.providers import YahooProvider, verified_instrument, yahoo_metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    checks = []

    def check(name, function):
        try:
            evidence = function()
            checks.append({"case": name, "status": "PASS", "observed": evidence})
        except Exception as exc:
            checks.append({"case": name, "status": "FAIL", "error_type": type(exc).__name__})

    def prices():
        split = yf.download("AAPL", start="2020-08-28", end="2020-09-02", **OPTIONS)
        close = float(split.loc["2020-08-28", ("Close", "AAPL")])
        factor = float(split.loc["2020-08-31", ("Stock Splits", "AAPL")])
        assert factor == 4 and math.isclose(close, 499.23 / 4, abs_tol=1e-4)
        dividends = yf.download(["AAPL", "MSFT"], start="2024-05-09", end="2024-05-15", **OPTIONS)
        before = float(dividends.loc["2024-05-09", ("Close", "AAPL")])
        after = float(dividends.loc["2024-05-10", ("Close", "AAPL")])
        dividend = float(dividends.loc["2024-05-10", ("Dividends", "AAPL")])
        ratio_before = float(dividends.loc["2024-05-09", ("Adj Close", "AAPL")]) / before
        ratio_after = float(dividends.loc["2024-05-10", ("Adj Close", "AAPL")]) / after
        assert math.isclose(before, 184.57, abs_tol=1e-4) and dividend == 0.25
        assert math.isclose(ratio_before / ratio_after, (before - dividend) / before, abs_tol=1e-6)
        assert all(s.date() < date(2024, 5, 15) for s in dividends.index)
        return {
            "split_session": "2020-08-31",
            "pre_split_raw_reference": 499.23,
            "split_factor": factor,
            "observed_split_adjusted_close": close,
            "dividend_session": "2024-05-10",
            "dividend": dividend,
            "pre_dividend_raw_reference": 184.57,
            "observed_close": before,
            "adj_multiplier_ratio": ratio_before / ratio_after,
            "expected_dividend_multiplier": (before - dividend) / before,
            "multi_symbols": list(dividends.columns.get_level_values(1).unique()),
            "exclusive_end": True,
        }

    check("dated_split_dividend_prices_and_shapes", prices)
    for symbol in ("AAPL", "MSFT", "SPY"):

        def metadata(symbol=symbol):
            value = yahoo_metadata(symbol, 15)
            instrument = verified_instrument(YahooProvider().resolve(symbol), value)
            return {
                "symbol": symbol,
                "exchange": value.get("exchangeName"),
                "timezone": value.get("exchangeTimezoneName"),
                "currency": instrument.currency,
                "type": instrument.instrument_type,
            }

        check("metadata_" + symbol, metadata)
    report = {
        "at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": {n: version(n) for n in ("yfinance", "pandas", "numpy")},
        "checks": checks,
        "intraday_live_observation": "NOT_PERFORMED; completed-session-only scope per ADR 0004",
        "sources": {
            "split_event": "https://www.apple.com/newsroom/2020/07/apple-reports-third-quarter-results/",
            "split_price": "https://www.firstcitizensgroup.com/tt/news-insights/a-closer-look-at-stock-splits/",
            "dividend_event": "https://investor.apple.com/dividend-history/default.aspx",
            "dividend_price": "https://stockinvest.us/stock-news/apple-inc-aapl-shows-bullish-signals-despite-overbought-conditions",
            "vendor_basis": "https://finance.yahoo.com/quote/AAPL/history/",
        },
    }
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if all(c["status"] == "PASS" for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
