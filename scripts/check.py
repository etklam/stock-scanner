#!/usr/bin/env python3
"""Local quality gate; the replacement for the removed GitHub Actions workflow.

Runs the same steps CI used to run, in order, on one machine:
runtime preflight -> ruff check -> ruff format --check -> mypy -> offline
pytest -> OpenAPI snapshot check -> uv build -> installed-wheel smoke.

`--fast` skips the build and wheel smoke (no compilation involved, so this is
a dependency-free convenience flag, not a weaker gate) and is a development
quick-check only: it does NOT stand in for release acceptance. The FULL gate
preflights the actual interpreter — printing the Python and SQLite runtimes
that will run the tests — and FAILS on a SQLite runtime known to be affected
by the WAL-reset bug (sqlite.org/wal.html#walresetbug), because a release
accepted on an affected runtime would be accepted against a known blocker.
Run the full gate on every platform you own before cutting a release; a
single machine cannot stand in for cross-platform verification.
"""

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qscan.adapters.diagnostics import sqlite_wal_reset_status  # noqa: E402

FAST_STEPS: list[tuple[str, list[str]]] = [
    ("ruff check", ["uv", "run", "ruff", "check", "."]),
    ("ruff format --check", ["uv", "run", "ruff", "format", "--check", "."]),
    ("mypy", ["uv", "run", "mypy"]),
    ("pytest (offline)", ["uv", "run", "pytest", "-m", "not online", "-q"]),
    (
        "OpenAPI contract check",
        ["uv", "run", "python", "scripts/openapi_snapshot.py", "--check"],
    ),
    ("frontend (dev quick-check)", [sys.executable, "scripts/frontend_gate.py"]),
]
RELEASE_STEPS = [
    # The frontend build MUST precede uv build: the wheel ships the compiled UI.
    ("frontend (release build)", [sys.executable, "scripts/frontend_gate.py", "--release"]),
    ("uv build", ["uv", "build"]),
    ("installed-wheel smoke", ["uv", "run", "python", "scripts/wheel_smoke.py"]),
]


def preflight(fast: bool) -> bool:
    """Record the actually-loaded runtimes; full gate fails on an affected SQLite.

    This checks THIS interpreter — the one that runs the gate steps — not the
    shell's nominal python. The installed-wheel smoke additionally verifies the
    runtime identity inside its own fresh venv.
    """
    print(f"runtime: python {sys.version.split()[0]} at {sys.executable}", flush=True)
    status = sqlite_wal_reset_status(sqlite3.sqlite_version)
    print(f"runtime: sqlite {sqlite3.sqlite_version} - {status}", flush=True)
    if status.startswith("AFFECTED"):
        if fast:
            print(
                "WARNING: affected SQLite runtime; the dev fast-check is not "
                "release acceptance — the full gate would fail here.",
                flush=True,
            )
            return True
        print(
            "FAIL release preflight: the full gate refuses to accept a release "
            "on an affected SQLite runtime; use a Python build with the fix "
            "(see sqlite.org/wal.html#walresetbug).",
            flush=True,
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip uv build and the installed-wheel smoke (dev quick-check only)",
    )
    arguments = parser.parse_args()
    if not preflight(arguments.fast):
        return 2
    steps = FAST_STEPS + ([] if arguments.fast else RELEASE_STEPS)
    started = perf_counter()
    for name, command in steps:
        step_started = perf_counter()
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode != 0:
            print(f"FAIL {name} (exit {completed.returncode})")
            return completed.returncode or 1
        print(f"ok   {name} ({perf_counter() - step_started:.1f}s)", flush=True)
    print(f"gate passed in {perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
