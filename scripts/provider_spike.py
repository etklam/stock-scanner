"""Run bounded live diagnostics without retaining bulk third-party price history."""

import argparse
import json
import platform
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pandas as pd
import yfinance as yf

from qscan.adapters.provider_release import yahoo_release

OPTIONS = dict(
    interval="1d",
    auto_adjust=False,
    back_adjust=False,
    repair=False,
    prepost=False,
    actions=True,
    threads=False,
    progress=False,
    timeout=15,
    keepna=True,
    group_by="column",
    multi_level_index=True,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    checks = []
    cases = [
        ("split_single", ["AAPL"], "2020-08-28", "2020-09-02"),
        ("dividend_multi", ["AAPL", "MSFT"], "2024-05-09", "2024-05-15"),
    ]
    for name, tickers, start, end in cases:
        record = {"case": name, "tickers": tickers, "start": start, "end_exclusive": end}
        try:
            frame = yf.download(tickers, start=start, end=end, **OPTIONS)
            if frame is None or frame.empty:
                raise ValueError("Provider returned no rows")
            if not isinstance(frame.columns, pd.MultiIndex):
                raise ValueError("Expected multi-level columns")
            fields = sorted(set(frame.columns.get_level_values(0)))
            record["fields"] = fields
            record["rows"] = len(frame)
            end_ok = all(stamp.date().isoformat() < end for stamp in frame.index)
            record["end_exclusive_pass"] = end_ok
            symbol_checks = {}
            for ticker in tickers:
                close = frame["Close"][ticker].dropna()
                adjusted = frame["Adj Close"][ticker].dropna()
                if close.empty or adjusted.empty:
                    raise ValueError(f"Missing Close or Adj Close for {ticker}")
                symbol_checks[ticker] = {
                    "close_positive_finite": bool(((close > 0) & (close < float("inf"))).all()),
                    "close_differs_from_adj_close": bool((close != adjusted).any()),
                }
            if name == "split_single":
                ratio = float(
                    frame.loc["2020-08-31", ("Close", "AAPL")]
                    / frame.loc["2020-08-28", ("Close", "AAPL")]
                )
                event = float(frame.loc["2020-08-31", ("Stock Splits", "AAPL")])
                record["split_event_is_four"] = event == 4
                record["split_continuity_pass"] = 0.8 < ratio < 1.2
            else:
                record["dividend_event_present"] = bool(
                    frame.loc["2024-05-10", ("Dividends", "AAPL")] > 0
                )
            record["symbols"] = symbol_checks
            flags = [end_ok]
            flags.extend(value for details in symbol_checks.values() for value in details.values())
            flags.extend(
                value
                for key, value in record.items()
                if key in {"split_event_is_four", "split_continuity_pass", "dividend_event_present"}
            )
            record["status"] = "PASS" if all(flags) else "FAIL"
        except Exception as exc:
            record["status"] = "FAIL"
            record["error"] = f"{type(exc).__name__}: {exc}"
        checks.append(record)
    report = {
        "requested_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "os": platform.platform(),
        "versions": {name: version(name) for name in ["yfinance", "pandas", "numpy"]},
        "options": OPTIONS,
        "checks": checks,
        "incomplete_session": "NOT_VERIFIED: requires an intraday observation and calendar cutoff",
        "price_basis": "REVIEW_REQUIRED: continuity and differing fields alone do not prove basis",
        "online_provider_release": yahoo_release().model_dump(mode="json"),
        "scope": "This mechanical probe alone does not change the release decision",
    }
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if all(check["status"] == "PASS" for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
