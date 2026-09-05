"""Phase 5 review export: seeded sampling, side lists, pending labels only."""

import csv
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import RawPrices
from qscan.bootstrap import bootstrap

SESSIONS = NYSECalendar().sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
CLOSES = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
SRC_ROOT = str(Path(__file__).parents[2] / "src")


def seed_with_mixed_symbols(directory: Path) -> str:
    """GOOD rises (candidate), FLAT/DOWN evaluate but never become candidates."""
    app = bootstrap(
        FixtureProvider(
            {
                "GOOD": RawPrices(tuple(zip(SESSIONS, CLOSES, strict=True))),
                "FLAT": RawPrices(tuple(zip(SESSIONS, (100.0,) * len(SESSIONS), strict=True))),
                "DOWN": RawPrices(
                    tuple(
                        zip(SESSIONS, (200.0 - i * 0.2 for i in range(len(SESSIONS))), strict=True)
                    )
                ),
            }
        ),
        data_dir=directory,
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=NYSECalendar(),
    )
    try:
        watchlist = app.watchlists.create("review", ["GOOD", "FLAT", "DOWN"])
        return str(app.scans.scan(watchlist.id, as_of=date(2026, 9, 4)).id)
    finally:
        app.close()


def run_cli(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from qscan.interfaces.cli import app; app()",
            "--provider",
            "fixture",
            "--data-dir",
            str(arguments[0]),
            *arguments[1:],
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": SRC_ROOT},
    )


def test_review_export_samples_and_pending_labels(tmp_path):
    directory = tmp_path / "資料 review"
    scan_id = seed_with_mixed_symbols(directory)

    first = run_cli(
        directory,
        "scans",
        "review-export",
        scan_id,
        "--output",
        str(tmp_path / "a.csv"),
        "--non-candidates",
        "1",
        "--seed",
        "7",
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    summary = json.loads(first.stdout)
    assert summary["candidate_count"] == 1  # GOOD only
    assert summary["non_candidate_pool"] == 1  # DOWN evaluates; FLAT is flagged suspicious-flat
    assert summary["excluded_count"] == 0
    assert summary["data_errors"] == [
        {"instrument": "FLAT", "reasons": "SUSPICIOUS_FLAT_SERIES"}
    ]  # data errors are listed separately, never in the sample pool
    assert summary["non_candidate_sampled"] == 1
    assert summary["labels_pending"] == ["worth_reviewing", "borderline", "not_useful"]

    with (tmp_path / "a.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2  # one candidate + one sampled control
    candidates = [row for row in rows if row["rank"]]
    controls = [row for row in rows if not row["rank"]]
    assert len(candidates) == 1 and candidates[0]["instrument"] == "GOOD"
    assert len(controls) == 1
    assert all(row["label"] == "" and row["notes"] == "" for row in rows)  # human pending
    assert rows[0]["date"] == "2026-09-04" and rows[0]["scan_id"] == scan_id

    # Same seed replays the same sheet; a different seed may draw a different control.
    again = run_cli(
        directory,
        "scans",
        "review-export",
        scan_id,
        "--output",
        str(tmp_path / "b.csv"),
        "--non-candidates",
        "1",
        "--seed",
        "7",
    )
    assert (tmp_path / "b.csv").read_bytes() == (tmp_path / "a.csv").read_bytes()
    assert again.returncode == 0

    full = run_cli(
        directory,
        "scans",
        "review-export",
        scan_id,
        "--output",
        str(tmp_path / "c.csv"),
        "--non-candidates",
        "5",
        "--seed",
        "3",
    )
    summary_full = json.loads(full.stdout)
    assert summary_full["non_candidate_sampled"] == 1  # capped by the one-symbol pool

    unfinished = run_cli(
        directory,
        "scans",
        "review-export",
        "00000000-0000-0000-0000-000000000000",
        "--output",
        str(tmp_path / "d.csv"),
    )
    assert unfinished.returncode == 2
    assert json.loads(unfinished.stdout)["error"]["code"] == "NOT_FOUND"
