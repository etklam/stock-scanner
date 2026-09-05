"""Executable offline acceptance example, also available from an installed wheel."""

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import RawPrices
from qscan.bootstrap import bootstrap
from qscan.domain.models import DataMode, RunState


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    calendar = NYSECalendar()
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-129:]
    closes = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0]
    provider = FixtureProvider({"DEMO": RawPrices(tuple(zip(sessions, closes, strict=True)))})
    app = bootstrap(provider, data_dir=args.data_dir, clock=clock, calendar=calendar)
    try:
        watchlist = app.watchlists.import_content(
            "demo-" + uuid4().hex[:12], b"# Synthetic only\nDEMO"
        )
        run = app.scans.scan(watchlist.id, mode=DataMode.FORCE)
        assert run.state == RunState.SUCCEEDED and run.counts.candidate == 1
        identity = run.id
    finally:
        app.close()
    # Reopen with an empty provider: replay must use only the immutable snapshot.
    offline = FixtureProvider({})
    app = bootstrap(offline, data_dir=args.data_dir, clock=clock, calendar=calendar)
    try:
        saved = app.queries.get(identity)
        replay = app.scans.replay(identity)
        cached = app.scans.scan(watchlist.id, mode=DataMode.CACHE_ONLY)
        assert saved.results == replay.results
        assert saved.results[0].analysis == cached.results[0].analysis
        assert not offline.calls
        print(
            json.dumps(
                {
                    "scan_id": str(saved.id),
                    "state": saved.state.value,
                    "counts": saved.counts.model_dump(),
                    "input_hash": saved.input_hash,
                    "replay_id": str(replay.id),
                    "replay_matches": saved.results == replay.results,
                    "results": [
                        {
                            "symbol": r.instrument.display_symbol,
                            "score": r.analysis.score,
                            "stage": r.analysis.stage,
                            "rank": r.rank,
                        }
                        for r in saved.results
                    ],
                },
                indent=2,
            )
        )
    finally:
        app.close()


if __name__ == "__main__":
    main()
