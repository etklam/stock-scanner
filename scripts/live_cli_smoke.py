"""Explicit small live CLI acceptance; only summaries/hashes are published."""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    commands = []

    def invoke(*parts):
        command = [
            str(
                Path(sys.executable).parent / ("qscan.exe" if sys.platform == "win32" else "qscan")
            ),
            "--data-dir",
            str(args.data_dir),
            "--provider",
            "yahoo",
            *parts,
        ]
        response = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", timeout=240
        )
        value = json.loads(response.stdout)
        commands.append({"args": list(parts), "exit": response.returncode})
        if response.returncode not in (0, 3):
            raise RuntimeError(json.dumps({"commands": commands, "result": value}))
        return value

    invoke("init")
    listing = args.data_dir / "personal.txt"
    listing.write_text("AAPL\nMSFT\nSPY\n", encoding="utf-8")
    watchlist = invoke("watchlist", "import", "--name", "live-acceptance", "--file", str(listing))
    refresh = invoke("data", "refresh", "--watchlist", watchlist["id"], "--as-of", args.as_of)
    run = invoke("scan", "--watchlist", watchlist["id"], "--as-of", args.as_of, "--format", "json")
    report_dir = args.data_dir / "reports"
    for kind in ("json", "csv", "html"):
        invoke("report", run["id"], "--format", kind, "--output", str(report_dir))
    cached = invoke(
        "scan",
        "--watchlist",
        watchlist["id"],
        "--as-of",
        args.as_of,
        "--data-mode",
        "cache_only",
        "--format",
        "json",
    )
    replay = invoke("replay", run["id"], "--format", "json")
    assert run["results"] == replay["results"]
    assert run["results"] == cached["results"]
    assert run["counts"]["evaluated"] == 3
    evidence = {
        "at": datetime.now(UTC).isoformat(),
        "as_of": args.as_of,
        "commands": commands,
        "state": run["state"],
        "counts": run["counts"],
        "metadata": [r["instrument"] for r in run["results"]],
        "input_hash": run["input_hash"],
        "cache_input_hash": cached["input_hash"],
        "replay_input_hash": replay["input_hash"],
        "replay_exact_results": True,
        "cache_equal_results": True,
        "refresh_successful": refresh["successful"],
    }
    args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
