#!/usr/bin/env python3
"""Frontend gate for web/ (React + TypeScript + Vite), cross-platform.

Dev quick-check (default): typecheck + lint + unit tests. Requires node/npm;
prints a clear SKIP notice when they are absent because a dev machine without
Node is still able to run the Python-only CLI/API.

Release mode (--release): additionally runs the PRODUCTION BUILD, which
places the compiled assets into src/qscan/interfaces/web/dist so the wheel
ships them. A release gate without a built UI is a failure, not a pass.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
FAST_STEPS = ["typecheck", "lint", "test"]
RELEASE_EXTRA = ["build"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true", help="Also run the production build")
    arguments = parser.parse_args()
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        message = (
            "node/npm not found on PATH; the frontend gate cannot run "
            f"({WEB}). Install Node 22+ to build or check the UI."
        )
        if arguments.release:
            print(f"FAIL frontend: {message}")
            return 2
        print(f"SKIP frontend (dev quick-check): {message}")
        return 0
    if not (WEB / "node_modules").is_dir():
        install = subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=WEB)
        if install.returncode != 0:
            print("FAIL frontend: npm ci failed")
            return install.returncode or 1
    steps = FAST_STEPS + (RELEASE_EXTRA if arguments.release else [])
    for name in steps:
        completed = subprocess.run([npm, "run", name], cwd=WEB)
        if completed.returncode != 0:
            print(f"FAIL frontend {name} (exit {completed.returncode})")
            return completed.returncode or 1
        print(f"ok   frontend {name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
