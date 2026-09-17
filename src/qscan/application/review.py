"""Human-review sample export and per-run review labels: screening aids only.

The sample sheet (review_samples/write_review_csv) builds a small review
sheet from one finished run: every ranked candidate plus a fixed-seed random
sample of evaluated non-candidates as the control group. Data errors and
exclusions are listed separately and never enter the sample pool. Nothing
here infers or scores usefulness, and no precision/recall is claimed anywhere.

ReviewService stores the minimal mutable labels a reviewer assigns per
(run, instrument). They are user data, not scan results: saving one never
changes scores, ranks, hashes or snapshots, labels are never carried to
another run automatically, and an exact replay starts unlabeled.
"""

import csv
import os
import random
from pathlib import Path
from typing import Any
from uuid import UUID

from qscan.application.contracts import (
    ApplicationError,
    Repository,
    ReviewLabel,
    Run,
    SavedReview,
    ScanResult,
)
from qscan.domain.models import ErrorCode, RunState

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
    "review_revision",
    "review_updated_at",
)


class ReviewService:
    """Minimal typed storage for human review labels, scoped to one run."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def list(self, identity: UUID) -> tuple[SavedReview, ...]:
        """All saved labels for one owned run (batch: one query for the table)."""
        self.repository.run_summary(identity)  # owner-scoped existence check
        return self.repository.reviews_for_run(identity)

    def save(
        self,
        identity: UUID,
        instrument_id: UUID,
        label: ReviewLabel,
        note: str,
        expected_revision: int | None,
    ) -> SavedReview:
        run: Run = self.repository.run(identity)
        if run.state not in (RunState.SUCCEEDED, RunState.PARTIAL):
            raise ApplicationError(
                ErrorCode.SCAN_NOT_READY, "Reviews need a finished run with published results"
            )
        matches = [r for r in run.results if r.instrument.id == instrument_id]
        if not matches:
            raise ApplicationError(ErrorCode.NOT_FOUND)
        if matches[0].category != "evaluated":
            # A data failure is not a verdict on the instrument; labeling one
            # would dress an evaluation gap up as human judgement.
            raise ApplicationError(
                ErrorCode.VALIDATION_ERROR,
                "Only instruments with a valid evaluation can be labeled",
            )
        return self.repository.save_review(identity, instrument_id, label, note, expected_revision)


def review_samples(
    run: Run,
    *,
    non_candidates: int,
    seed: int,
    reviews: tuple[SavedReview, ...] = (),
) -> dict[str, Any]:
    """Split one run's results into review rows and side lists.

    Saved labels are merged in when present: rows already on the sheet get
    their label/notes filled, and labeled instruments outside the sampled
    sheet are appended so an export never quietly drops a human decision.
    """
    saved = {review.instrument_id: review for review in reviews}
    # The persisted contract normally guarantees one result per instrument,
    # but keep exports identity-keyed even when an old/corrupt document has
    # duplicate rows. Preserve the first row so fixed-seed sampling and the
    # candidate/control ordering remain unchanged for valid runs.
    unique_results: list[ScanResult] = []
    seen_instruments: set[UUID] = set()
    for result in run.results:
        identity = result.instrument.id
        if identity in seen_instruments:
            continue
        seen_instruments.add(identity)
        unique_results.append(result)
    candidates = sorted(
        (r for r in unique_results if r.analysis.is_candidate), key=lambda r: r.rank or 0
    )
    evaluated_non = sorted(
        (r for r in unique_results if r.category == "evaluated" and not r.analysis.is_candidate),
        key=lambda r: str(r.instrument.id),
    )
    chosen = random.Random(seed).sample(evaluated_non, min(non_candidates, len(evaluated_non)))

    def row(result: ScanResult) -> dict[str, Any]:
        window = result.analysis.selected_window
        review = saved.get(result.instrument.id)
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
            "label": review.label if review else "",
            "notes": review.note if review else "",
            "review_revision": review.revision if review else "",
            "review_updated_at": review.updated_at.isoformat() if review else "",
        }

    rows = [row(result) for result in (*candidates, *chosen)]
    # Rows are serialized for CSV, so compare the same string identity type
    # when deciding whether an already-exported review needs appending.
    sheet_ids = {str(r["instrument_id"]) for r in rows}
    # Labeled instruments missing from candidates/sample keep their verdicts.
    evaluated = [r for r in unique_results if r.category == "evaluated"]
    rows.extend(
        row(result)
        for result in evaluated
        if result.instrument.id in saved and str(result.instrument.id) not in sheet_ids
    )
    return {
        "columns": list(_COLUMNS),
        "rows": rows,
        "candidate_count": len(candidates),
        "non_candidate_pool": len(evaluated_non),
        "non_candidate_sampled": len(chosen),
        "excluded_count": sum(1 for r in unique_results if r.category == "excluded"),
        "data_errors": [
            {
                "instrument": r.instrument.display_symbol,
                "reasons": ",".join(reason.code for reason in r.analysis.reasons),
            }
            for r in unique_results
            if r.category == "data_error"
        ],
        "labels": list(LABELS),
        "labeled_count": len(saved),
    }


def write_review_csv(samples: dict[str, Any], output: str | os.PathLike[str]) -> Path:
    path = Path(os.fspath(output))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=samples["columns"])
        writer.writeheader()
        writer.writerows(samples["rows"])
    return path
