"""Read-only environment checks with explicit bounded online diagnostics."""

import os
import sqlite3
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
from sqlalchemy import inspect, text

from qscan.adapters.calendar import NYSECalendar
from qscan.adapters.provider_release import yahoo_release
from qscan.adapters.providers import YahooProvider
from qscan.application.contracts import ApplicationError
from qscan.domain.models import Contract


class Diagnosis(Contract):
    local_healthy: bool
    initialized: bool
    checks: dict[str, str]
    yahoo_release: str
    release_blockers: tuple[str, ...]
    release_limitations: tuple[str, ...]
    online: str = "NOT_REQUESTED"
    sqlite_runtime: str = ""


# Verified 2026-09-06 against sqlite.org/news.html and sqlite.org/wal.html
# (#walresetbug, found 2026-03-03): the WAL-reset bug is fixed in 3.51.3;
# 3.50.7 and 3.44.6 are fixed backports on their own release branches. Any
# OTHER version below 3.51.3 is reported AFFECTED — including other 3.50.x
# patch levels that are not the 3.50.7 backport. This list is a dated
# snapshot, not a live feed; re-verify before each release.
_SQLITE_WAL_RESET_FIXED = frozenset({"3.51.3", "3.50.7", "3.44.6"})


def sqlite_wal_reset_status(version: str) -> str:
    parts = tuple(int(piece) for piece in version.split("."))
    if parts >= (3, 51, 3) or version in _SQLITE_WAL_RESET_FIXED:
        return "OK (includes the WAL-reset fix)"
    return (
        "AFFECTED by the SQLite WAL-reset bug (rare corruption risk); "
        "use a Python build shipping 3.51.3+ or the 3.50.7/3.44.6 backports "
        "- see sqlite.org/wal.html#walresetbug (checked 2026-09-06)"
    )


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
    # Local health covers the core checks; the SQLite advisory is surfaced
    # separately so an affected runtime is visible without hiding other
    # diagnostics, and release acceptance reads it as its own gate.
    local_healthy = all(value == "OK" for value in checks.values())
    sqlite_version = sqlite3.sqlite_version
    sqlite_report = f"{sqlite_version} - {sqlite_wal_reset_status(sqlite_version)}"
    checks["sqlite_runtime"] = sqlite_report
    return Diagnosis(
        local_healthy=local_healthy,
        initialized=initialized,
        checks=checks,
        online=online_result,
        yahoo_release=yahoo_release().status,
        release_blockers=yahoo_release().blockers,
        release_limitations=yahoo_release().limitations,
        sqlite_runtime=sqlite_report,
    )
