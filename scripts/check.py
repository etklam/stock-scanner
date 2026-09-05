#!/usr/bin/env python3
"""Local quality gate; the replacement for the removed GitHub Actions workflow.

Runs the same steps CI used to run, in order, on one machine:
ruff check -> ruff format --check -> mypy -> offline pytest ->
OpenAPI snapshot check -> uv build -> installed-wheel smoke.

`--fast` skips the build and wheel smoke (no compilation involved, so this is
a dependency-free convenience flag, not a weaker gate). Run the full gate on
every platform you own before cutting a release; a single machine cannot
stand in for cross-platform verification.
"""

import argparse
import subprocess
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]

FAST_STEPS: list[tuple[str, list[str]]] = [
    ("ruff check", ["uv", "run", "ruff", "check", "."]),
    ("ruff format --check", ["uv", "run", "ruff", "format", "--check", "."]),
    ("mypy", ["uv", "run", "mypy"]),
    ("pytest (offline)", ["uv", "run", "pytest", "-m", "not online", "-q"]),
    ("OpenAPI contract check", ["uv", "run", "python", "scripts/openapi_snapshot.py", "--check"]),
]
RELEASE_STEPS = [
    ("uv build", ["uv", "build"]),
    ("installed-wheel smoke", ["uv", "run", "python", "scripts/wheel_smoke.py"]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip uv build and the installed-wheel smoke",
    )
    arguments = parser.parse_args()
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
