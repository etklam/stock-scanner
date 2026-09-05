"""Reproducible synthetic report benchmark; no network or strategy changes."""

import argparse
import json
import platform
import tempfile
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.adapters.report_renderer import render
from qscan.application.contracts import RawPrices
from qscan.bootstrap import bootstrap


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    measurements = []
    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2024, 1, 1), date(2026, 9, 4))[-504:]
    closes = (
        [20 + i * 0.08 for i in range(375)]
        + [50 + i * 0.5 for i in range(88)]
        + [99.0, 100.0] * 20
        + [100.0]
    )
    raw = RawPrices(tuple(zip(sessions, closes, strict=True)))
    for count in (100, 1000):
        with tempfile.TemporaryDirectory(prefix="qscan benchmark ") as temporary:
            symbols = [f"S{i}" for i in range(count)]
            app = bootstrap(
                FixtureProvider(dict.fromkeys(symbols, raw)),
                data_dir=Path(temporary),
                calendar=calendar,
                clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
            )
            try:
                watchlist = app.watchlists.import_content(
                    "SYNTHETIC benchmark", "\n".join(symbols).encode()
                )
                run = app.scans.scan(watchlist.id)
                start = perf_counter()
                run = app.repository.run(run.id)
                loaded = perf_counter()
                snapshot = app.reports.load_snapshot(run)
                decoded = perf_counter()
                charts = app.reports.chart_data(
                    run, snapshot, tuple(r.instrument.id for r in run.results[:30])
                )
                charted = perf_counter()
                report = app.reports.build(run.id, include_charts=False).model_copy(
                    update={"charts": charts, "charts_included": True}
                )
                before_render = perf_counter()
                payload = render(report, "html")
                rendered = perf_counter()
                measurements.append(
                    {
                        "symbols": count,
                        "sessions": 504,
                        "charts": len(charts),
                        "run_load_seconds": loaded - start,
                        "snapshot_decode_seconds": decoded - loaded,
                        "chart_data_seconds": charted - decoded,
                        "render_seconds": rendered - before_render,
                        "html_bytes": len(payload),
                    }
                )
            finally:
                app.close()
    args.output.write_text(
        json.dumps(
            {
                "label": "SYNTHETIC; single pass, not a throughput guarantee",
                "python": platform.python_version(),
                "platform": platform.platform(),
                "versions": {
                    n: version(n) for n in ("matplotlib", "pandas", "numpy", "sqlalchemy")
                },
                "measurements": measurements,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
