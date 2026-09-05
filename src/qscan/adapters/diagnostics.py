"""Read-only environment checks with explicit bounded online diagnostics."""

import os
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
from sqlalchemy import inspect, text

from qscan.adapters.calendar import NYSECalendar
from qscan.adapters.providers import YahooProvider
from qscan.application.contracts import ApplicationError
from qscan.domain.models import Contract


class Diagnosis(Contract):
    local_healthy: bool
    initialized: bool
    checks: dict[str, str]
    yahoo_release: str = "BLOCKED"
    release_blockers: tuple[str, ...] = (
        "Manual split/dividend Close price-basis review",
        "Live incomplete-session observation",
        "Safe exchange/currency/instrument metadata verification",
    )
    online: str = "NOT_REQUESTED"


def diagnose(directory: Path, online: bool = False) -> Diagnosis:
    from qscan.bootstrap import bootstrap

    initialized = (directory / "qscan.sqlite3").is_file()
    checks = {
        "data_directory": "OK" if directory.is_dir() else "NOT_INITIALIZED",
        "permissions": "OK" if os.access(directory, os.R_OK | os.W_OK) else "UNAVAILABLE",
        "configuration": "OK",
        "database": "NOT_INITIALIZED",
        "snapshots": "OK" if (directory / "snapshots").is_dir() else "NOT_INITIALIZED",
        "lock": "NOT_INITIALIZED",
    }
    try:
        ZoneInfo("America/New_York")
        calendar = NYSECalendar()
        assert calendar.sessions(date(2026, 9, 3), date(2026, 9, 4))
        checks["timezone_calendar"] = "OK"
    except Exception:
        checks["timezone_calendar"] = "UNAVAILABLE"
    if initialized:
        try:
            app = bootstrap(YahooProvider(), data_dir=directory, initialize=False, readonly=True)
            try:
                required = {
                    "scan_runs",
                    "scan_results",
                    "watchlists",
                    "watchlist_members",
                    "instruments",
                    "cache_by_provider",
                    "prices_by_provider",
                }
                with app.engine.connect() as connection:
                    integrity = connection.scalar(text("PRAGMA quick_check"))
                checks["database"] = (
                    "OK"
                    if (
                        required <= set(inspect(app.engine).get_table_names()) and integrity == "ok"
                    )
                    else "SCHEMA_UNAVAILABLE"
                )
            finally:
                app.close()
        except Exception:
            checks["database"] = "SCHEMA_UNAVAILABLE"
        # Lock acquisition never deletes the lock or changes run state.
        try:
            with FileLock(directory / "executor.lock", timeout=0):
                checks["lock"] = "OK"
        except Timeout:
            checks["lock"] = "BUSY"
        except OSError:
            checks["lock"] = "UNAVAILABLE"
    online_result = "NOT_REQUESTED"
    if online:
        provider = YahooProvider(diagnostic=True, attempts=1, timeout=5)
        try:
            raw = provider.fetch(provider.resolve("AAPL"), date(2024, 6, 3), date(2024, 6, 7))
            online_result = "MECHANICAL_RESPONSE_ONLY" if raw.rows else "NO_DATA"
        except ApplicationError as exc:
            online_result = exc.code.value
    return Diagnosis(
        local_healthy=all(v == "OK" for v in checks.values()),
        initialized=initialized,
        checks=checks,
        online=online_result,
    )
