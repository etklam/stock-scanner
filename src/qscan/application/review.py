"""Human-review sample export: a simple screening-aid, not a performance claim.

Builds a small review sheet from one finished run: every ranked candidate plus
a fixed-seed random sample of evaluated non-candidates as the control group.
Data errors and exclusions are listed separately and never enter the sample
pool. Label and notes columns are left empty for a human; nothing here infers
or scores usefulness, and no precision/recall is claimed anywhere.
"""

import csv
import os
import random
from pathlib import Path
from typing import Any

from qscan.application.contracts import Run, ScanResult

LABELS = ("worth_reviewing", "borderline", "not_useful")
_COLUMNS = (
    "date",
    "scan_id",
    "instrument",
    "instrument_id",
    "window_sessions",
    "score",
    "stage",
    "rank",
    "reasons",
    "label",
    "notes",
)


def review_samples(run: Run, *, non_candidates: int, seed: int) -> dict[str, Any]:
    """Split one run's results into review rows and side lists."""
    candidates = sorted(
        (r for r in run.results if r.analysis.is_candidate), key=lambda r: r.rank or 0
    )
    evaluated_non = sorted(
        (r for r in run.results if r.category == "evaluated" and not r.analysis.is_candidate),
        key=lambda r: str(r.instrument.id),
    )
    chosen = random.Random(seed).sample(evaluated_non, min(non_candidates, len(evaluated_non)))

    def row(result: ScanResult) -> dict[str, Any]:
        window = result.analysis.selected_window
        return {
            "date": run.context.as_of_session.isoformat(),
            "scan_id": str(run.id),
            "instrument": result.instrument.display_symbol,
            "instrument_id": str(result.instrument.id),
            "window_sessions": window.window_sessions if window else "",
            "score": result.analysis.score if result.analysis.score is not None else "",
            "stage": result.analysis.stage.value if result.analysis.stage else "",
            "rank": result.rank if result.rank is not None else "",
            "reasons": ",".join(reason.code for reason in result.analysis.reasons),
            "label": "",
            "notes": "",
        }

    return {
        "columns": list(_COLUMNS),
        "rows": [row(result) for result in (*candidates, *chosen)],
        "candidate_count": len(candidates),
        "non_candidate_pool": len(evaluated_non),
        "non_candidate_sampled": len(chosen),
        "excluded_count": sum(1 for r in run.results if r.category == "excluded"),
        "data_errors": [
            {
                "instrument": r.instrument.display_symbol,
                "reasons": ",".join(reason.code for reason in r.analysis.reasons),
            }
            for r in run.results
            if r.category == "data_error"
        ],
        "labels": list(LABELS),
    }


def write_review_csv(samples: dict[str, Any], output: str | os.PathLike[str]) -> Path:
    path = Path(os.fspath(output))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=samples["columns"])
        writer.writeheader()
        writer.writerows(samples["rows"])
    return path
