"""Segmented synthetic benchmark for the local V1 release candidate.

Reuses the production scanner, storage and rendering stack; no strategy or
algorithm lives here. Segments are measured separately so fetch, core compute,
storage, report assembly and rendering are never blended into one number:

1. pure core + ranking (analyze_symbol / rank_candidates on synthetic series)
2. fixture/cache-only market-data validation (cold fetch, warm cache),
   snapshot write, and a full end-to-end scan for context
3. report data assembly (run load + snapshot decode + build without charts)
4. HTML rendering with fixed top-30 charts (cold includes one-time setup)
5. API behaviour under a controllable slow provider (small, correctness-oriented)
6. backup / verify / restore wall time and archive size

Method: warm-up pass first, then >=5 samples for the core segment (median is
the headline; raw samples included). Target from the development plan: median
core+ranking for 1,000 symbols x 504 sessions <= 10s on the reference machine.
That target is a goal, not a guarantee; CI never asserts absolute seconds.

Peak memory: each size runs in its own subprocess and reports
resource.getrusage(RUSAGE_SELF).ru_maxrss (KB after macOS bytes conversion;
Linux reports KB natively). This is process peak RSS including the Python
interpreter and native libraries; tracemalloc is NOT used because it cannot
see NumPy/native allocations. Windows has no resource module and reports null.
"""

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime
from importlib.metadata import version as dep_version
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
SIZES = (100, 500, 1000, 2000)
CORE_SAMPLES = 5
PIPELINE_SAMPLES = 3
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED"}
MEASUREMENT_NOTES = [
    "Timed with time.perf_counter around end-to-end calls; median of raw samples reported.",
    "Core segment: warm-up pass excluded, then 5 timed passes over all symbols.",
    "Fixture provider is in-memory; segment 2 'validation' covers the real quality "
    "gate but no network. Online Yahoo performance is a different measurement.",
    "Render cold includes one-time matplotlib font/backend setup; warm is the "
    "steady state users see on later reports.",
    "Peak RSS per size comes from a dedicated subprocess (RUSAGE_SELF.ru_maxrss); "
    "it includes interpreter + native libraries; tracemalloc would miss NumPy memory.",
    "Backup/verify/restore timings include hashing every entry and full "
    "integrity verification, not just copying.",
]


def synthetic_closes(index: int, sessions: tuple) -> tuple:
    """Deterministic mix: ~70% rising (candidate-capable), ~15% flat, ~15% short."""
    kind = index % 20
    if kind < 14:
        drift = 0.02 + (index % 7) * 0.004
        base = 20.0 + (index % 13)
        closes = [base * (1 + drift * i / 100) for i in range(len(sessions) - 130)]
        closes += [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
        return tuple(zip(sessions[-len(closes) :], closes, strict=True))
    if kind < 17:
        return tuple(zip(sessions, (100.0,) * len(sessions), strict=True))
    short = sessions[-60:]
    return tuple(zip(short, (50.0 + i * 0.1 for i in range(60)), strict=True))


def build_app(symbols: list[str], data_dir: Path):
    from qscan.adapters.calendar import FixedClock, NYSECalendar
    from qscan.adapters.providers import FixtureProvider
    from qscan.application.contracts import RawPrices
    from qscan.bootstrap import bootstrap

    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2024, 1, 1), date(2026, 9, 4))[-504:]
    data = {
        symbol: RawPrices(synthetic_closes(index, sessions)) for index, symbol in enumerate(symbols)
    }
    return (
        bootstrap(
            FixtureProvider(data),
            data_dir=data_dir,
            calendar=calendar,
            clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        ),
        sessions,
    )


def measure(call, samples: int) -> dict:
    call()  # warm-up, excluded
    raw = []
    for _ in range(samples):
        started = perf_counter()
        call()
        raw.append(perf_counter() - started)
    return {
        "samples_seconds": [round(value, 4) for value in raw],
        "median_seconds": round(statistics.median(raw), 4),
    }


def child(symbols_count: int) -> dict:
    """Run every size-local segment in this subprocess; report peak RSS too."""
    from qscan.adapters.providers import resolve_us
    from qscan.application.contracts import InputItem, InputSnapshot
    from qscan.core import analyze_symbol, rank_candidates
    from qscan.domain.models import CloseSeries, DataMode, ErrorCode
    from qscan.domain.rules import RuleConfig

    symbols = [f"S{index:04d}" for index in range(symbols_count)]
    report: dict[str, object] = {"symbols": symbols_count}
    with tempfile.TemporaryDirectory(prefix="qscan-benchmark-") as temporary:
        app, sessions = build_app(symbols, Path(temporary))
        try:
            context = app.scans.calendar.resolve(date(2026, 9, 4), app.clock.now())
            rules = RuleConfig()
            items = []
            for index, symbol in enumerate(symbols):
                instrument = resolve_us(symbol)
                rows = synthetic_closes(index, sessions)
                if len(rows) < 100:
                    items.append(
                        InputItem(instrument=instrument, error=ErrorCode.INSUFFICIENT_HISTORY)
                    )
                else:
                    series = CloseSeries(
                        instrument_id=instrument.id,
                        sessions=tuple(row[0] for row in rows),
                        closes=tuple(row[1] for row in rows),
                    )
                    items.append(InputItem(instrument=instrument, series=series))
            report["symbols"] = symbols_count
            report["sessions"] = len(sessions)

            # 1. pure core + ranking
            def core_pass():
                analyses = [
                    analyze_symbol(item.series, rules, context) if item.series is not None else None
                    for item in items
                ]
                rank_candidates([analysis for analysis in analyses if analysis is not None], rules)

            report["core_and_ranking"] = measure(core_pass, CORE_SAMPLES)

            # 2. market-data validation (cold fetch, warm cache), snapshot, e2e scan
            instruments = tuple(item.instrument for item in items)

            def obtain_all(mode: DataMode):
                for instrument in instruments:
                    app.market.obtain(instrument, sessions, mode)

            report["market_data_obtain_cold"] = measure(lambda: obtain_all(DataMode.AUTO), 1)
            report["market_data_obtain_warm"] = measure(
                lambda: obtain_all(DataMode.AUTO), PIPELINE_SAMPLES
            )

            def write_snapshot():
                from uuid import uuid4

                from qscan.application.contracts import Watchlist

                watchlist = Watchlist(
                    id=uuid4(),
                    name="snapshot-probe",
                    revision=1,
                    instruments=(items[0].instrument,),
                )
                snapshot = InputSnapshot(
                    context=context,
                    rules=rules,
                    watchlist=watchlist,
                    calendar_version=app.calendar.version,
                    expected_sessions=sessions[-504:],
                    items=tuple(items[:1]),
                )
                app.snapshots.write(snapshot)

            report["snapshot_write"] = measure(write_snapshot, PIPELINE_SAMPLES)

            watchlist = app.watchlists.import_content(
                "SYNTHETIC benchmark", "\n".join(symbols).encode()
            )

            def end_to_end_scan():
                app.scans.scan(watchlist.id, as_of=date(2026, 9, 4), mode=DataMode.FORCE)

            report["end_to_end_scan_force"] = measure(end_to_end_scan, PIPELINE_SAMPLES)

            # 3. report assembly + snapshot decode
            run_id = app.queries.page(limit=1)[0][0].id

            def assemble():
                loaded = app.repository.run(run_id)
                app.reports.load_snapshot(loaded)
                app.reports.build(run_id, include_charts=False)

            report["report_assembly"] = measure(assemble, PIPELINE_SAMPLES)

            # 4. rendering, cold vs warm, fixed top-30 charts
            from qscan.adapters.report_renderer import render

            full = app.reports.build(run_id, top=30, include_charts=True)
            started = perf_counter()
            payload = render(full, "html")
            report["render_html_cold_seconds"] = round(perf_counter() - started, 4)
            report["render_html_warm"] = measure(lambda: render(full, "html"), PIPELINE_SAMPLES)
            report["render_html_bytes"] = len(payload)

            # 6. backup / verify / restore on this size's data directory
            import shutil

            from qscan.adapters.backup import create_backup, restore_backup, verify_backup

            backup_area = Path(tempfile.mkdtemp(prefix="qscan-benchmark-backup-"))
            archive = backup_area / "benchmark-backup.zip"
            started = perf_counter()
            manifest = create_backup(Path(temporary), archive)
            report["backup_seconds"] = round(perf_counter() - started, 4)
            started = perf_counter()
            verify_backup(archive)
            report["verify_seconds"] = round(perf_counter() - started, 4)
            started = perf_counter()
            restore_backup(archive, backup_area / "restored")
            report["restore_seconds"] = round(perf_counter() - started, 4)
            report["archive_bytes"] = archive.stat().st_size
            report["runs"] = manifest["counts"]["runs"]
            shutil.rmtree(backup_area, ignore_errors=True)
        finally:
            app.close()
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["peak_rss_kb"] = int(peak / 1024) if sys.platform == "darwin" else int(peak)
        report["peak_rss_method"] = "getrusage(RUSAGE_SELF).ru_maxrss, subprocess-isolated"
    except ImportError:
        report["peak_rss_kb"] = None
        report["peak_rss_method"] = "unavailable on this platform (no resource module)"
    return report


def api_segment() -> dict:
    """Small slow-provider API check: queries stay served, queue stays honest."""
    import threading

    from fastapi.testclient import TestClient

    from qscan.executor import ScanExecutor
    from qscan.interfaces.api.app import create_app
    from qscan.interfaces.api.localauth import ensure_token

    with tempfile.TemporaryDirectory(prefix="qscan-benchmark-api-") as temporary:
        app, _sessions = build_app(
            [f"S{index:04d}" for index in range(100)], Path(temporary) / "data"
        )
        provider = app.provider
        original = provider.fetch
        release = threading.Event()

        def slow(instrument, start, end):
            release.wait(30)
            return original(instrument, start, end)

        provider.fetch = slow
        ensure_token(Path(temporary) / "t.json")
        token = json.loads((Path(temporary) / "t.json").read_text())["token"]
        executor = ScanExecutor(app, poll_seconds=0.05, stop_grace=5)
        fastapi = create_app(
            app,
            token_path=Path(temporary) / "t.json",
            executor=executor,
            allowed_hosts=("testserver",),
        )
        executor.start()
        status_latencies: list[float] = []
        try:
            with TestClient(fastapi) as client:
                headers = {"Authorization": f"Bearer {token}"}
                watchlist_id = client.post(
                    "/api/v1/watchlists",
                    headers=headers,
                    json={"name": "slow", "symbols": ["S0000", "S0001"]},
                ).json()["id"]
                submit_body = {
                    "watchlist_id": watchlist_id,
                    "as_of_session": "2026-09-04",
                    "data_mode": "force",
                }
                submit_started = perf_counter()
                scan_id = client.post(
                    "/api/v1/scans",
                    headers=headers | {"Idempotency-Key": "bench-slow-00000001"},
                    json=submit_body,
                ).json()["id"]
                accepted_seconds = perf_counter() - submit_started
                unblocked = False
                deadline = perf_counter() + 30
                while perf_counter() < deadline:
                    before = perf_counter()
                    response = client.get(f"/api/v1/scans/{scan_id}", headers=headers)
                    status_latencies.append(perf_counter() - before)
                    if response.json()["state"] in TERMINAL:
                        break
                    if not unblocked:
                        release.set()  # worker was parked; queries still served
                        unblocked = True
                final = client.get(f"/api/v1/scans/{scan_id}", headers=headers).json()
                results = client.get(
                    f"/api/v1/scans/{scan_id}/results", headers=headers, params={"limit": 200}
                ).json()
                assert len(results["items"]) == 2, "results must be complete after the slow scan"
                duplicate = client.post(
                    "/api/v1/scans",
                    headers=headers | {"Idempotency-Key": "bench-slow-00000001"},
                    json=submit_body,
                )
                assert duplicate.status_code == 200 and duplicate.json()["id"] == scan_id
        finally:
            release.set()
            executor.stop()
            app.close()
        return {
            "symbols": 2,
            "submit_accepted_seconds": round(accepted_seconds, 4),
            "status_get_median_seconds": round(statistics.median(status_latencies), 5),
            "status_get_samples": len(status_latencies),
            "final_state": final["state"],
            "notes": (
                "Worker parked in a controllable slow provider while status, results "
                "and idempotent replay stayed served; no duplicate queue entries."
            ),
        }


def environment() -> dict:
    import sqlite3

    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return {
        "commit": completed.stdout.strip() or "unknown (not a git checkout)",
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite_runtime": sqlite3.sqlite_version,
        "dependencies": {
            name: dep_version(name)
            for name in (
                "pydantic",
                "sqlalchemy",
                "fastapi",
                "uvicorn",
                "numpy",
                "pandas",
                "matplotlib",
                "alembic",
            )
        },
        "provider_mode": "fixture (SYNTHETIC, in-memory)",
        "seed": "deterministic-formula-v1 (no randomness)",
        "measured_at": datetime.now(UTC).isoformat(),
        "notes": MEASUREMENT_NOTES,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs" / "benchmarks" / "phase5-local.json"
    )
    parser.add_argument("--sizes", type=int, nargs="*", default=list(SIZES))
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--child", type=int, default=None, help=argparse.SUPPRESS)
    arguments = parser.parse_args()

    if arguments.child is not None:
        print(json.dumps(child(arguments.child)))
        return 0

    sizes = []
    for size in arguments.sizes:
        completed = subprocess.run(
            [sys.executable, __file__, "--child", str(size)],
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if completed.returncode != 0:
            print(completed.stderr, file=sys.stderr)
            return 1
        sizes.append(json.loads(completed.stdout.strip().splitlines()[-1]))
        print(f"measured {size} symbols", file=sys.stderr)
    document = {
        "label": "SYNTHETIC benchmark; medians are goals on the reference machine, not guarantees",
        "environment": environment(),
        "core_target": "1000x504 core+ranking median <= 10s (development-plan target)",
        "sizes": sizes,
    }
    if not arguments.skip_api:
        document["api_slow_provider"] = api_segment()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(document, indent=2) + "\n")
    print(f"benchmark written: {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
