"""Offline exports; render completely before atomic publication."""

import base64
import csv
import io
import json
import os
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Literal

from jinja2 import Environment, select_autoescape

from qscan.application.contracts import ApplicationError
from qscan.application.reporting import ChartSeries, Report
from qscan.domain.models import ErrorCode

CSV_FIELDS = (
    "scan_id",
    "as_of_session",
    "reference_session",
    "state",
    "source",
    "instrument_id",
    "symbol",
    "category",
    "rank",
    "score",
    "stage",
    "selected_window_sessions",
    "prior_move_ratio",
    "contraction_ratio",
    "resistance_distance_ratio",
    "score_breakdown",
    "reasons",
    "warnings",
    "data_label",
    "ruleset",
    "rules_version",
    "engine_version",
    "price_basis",
    "requested_count",
    "evaluated_count",
    "excluded_count",
    "data_error_count",
    "candidate_count",
    "previous_run_id",
    "comparison_codes",
    "limitation",
)


def csv_text(value: object) -> object:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def chart_png(chart: ChartSeries) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import numpy as np
    from matplotlib import pyplot as plt
    from matplotlib.dates import date2num

    dates = date2num(chart.sessions)  # type: ignore[no-untyped-call]

    figure, axis = plt.subplots(figsize=(9, 3.5), layout="constrained")
    try:
        axis.plot(dates, chart.closes, label="Close", color="#153e55", linewidth=1.6)
        for n, values in chart.sma.items():
            axis.plot(dates, np.asarray(values, dtype=float), label="SMA" + n, linewidth=1)
        if chart.window_start and chart.window_end:
            axis.axvspan(
                float(date2num(chart.window_start)),  # type: ignore[no-untyped-call]
                float(date2num(chart.window_end)),  # type: ignore[no-untyped-call]
                alpha=0.12,
                color="#b36d20",
                label="Selected window (ends at reference session)",
            )
        if chart.close_resistance is not None:
            axis.axhline(
                chart.close_resistance, linestyle="--", color="#905016", label="Close resistance"
            )
        axis.set_title(chart.symbol + " | split-adjusted Close")
        axis.xaxis_date()
        axis.set_ylabel("Close")
        axis.legend(fontsize=7, loc="best")
        axis.tick_params(axis="x", labelsize=8)
        axis.grid(axis="y", alpha=0.15)
        stream = io.BytesIO()
        figure.savefig(stream, format="png", dpi=120)
        return stream.getvalue()
    finally:
        plt.close(figure)


def render(
    report: Report, format: Literal["json", "csv", "html"], *, all_results: bool = False
) -> bytes:
    if format == "json":
        return report.model_dump_json(indent=2).encode("utf-8")
    if format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for result in report.run.results:
            if not all_results and result.rank is None:
                continue
            analysis = result.analysis
            window = analysis.selected_window
            features = window.features if window else {}
            row = dict(
                zip(
                    CSV_FIELDS[:18],
                    (
                        str(report.run.id),
                        report.run.context.as_of_session.isoformat(),
                        report.run.context.reference_session.isoformat(),
                        report.run.state.value,
                        result.provenance.provider if result.provenance else "",
                        str(result.instrument.id),
                        result.instrument.display_symbol,
                        result.category,
                        result.rank,
                        analysis.score,
                        analysis.stage,
                        window.window_sessions if window else None,
                        features.get("prior_move"),
                        analysis.features.get("contraction_ratio"),
                        features.get("distance_to_resistance"),
                        json.dumps(window.score_breakdown if window else {}, ensure_ascii=False),
                        json.dumps([r.model_dump() for r in analysis.reasons], ensure_ascii=False),
                        json.dumps(
                            result.warnings
                            + (result.provenance.warnings if result.provenance else ()),
                            ensure_ascii=False,
                        ),
                    ),
                    strict=True,
                )
            )
            row.update(
                {
                    "data_label": "SYNTHETIC / DEMO"
                    if result.provenance and result.provenance.provider == "fixture"
                    else "MARKET / UNVERIFIED",
                    "ruleset": report.run.rules.ruleset_id,
                    "rules_version": report.run.rules.version,
                    "engine_version": report.run.context.engine_version,
                    "price_basis": report.run.price_basis,
                    **{
                        key + "_count": value
                        for key, value in report.run.counts.model_dump().items()
                    },
                    "previous_run_id": str(report.comparison.previous_run_id or ""),
                    "comparison_codes": json.dumps(
                        [
                            code
                            for change in report.comparison.changes
                            if change.instrument_id in (None, result.instrument.id)
                            for code in change.codes
                        ]
                    ),
                    "limitation": report.limitation,
                }
            )
            writer.writerow({key: csv_text(value) for key, value in row.items()})
        return stream.getvalue().encode("utf-8-sig")
    environment = Environment(autoescape=select_autoescape(default=True))
    template = environment.from_string(
        files("qscan.adapters").joinpath("report.html").read_text(encoding="utf-8")
    )
    images = {
        str(c.instrument_id): base64.b64encode(chart_png(c)).decode("ascii") for c in report.charts
    }
    candidates = [r for r in report.run.results if r.rank is not None][
        : report.displayed_candidates
    ]
    return template.render(report=report, candidates=candidates, images=images).encode("utf-8")


def export(
    report: Report,
    directory: Path,
    format: Literal["json", "csv", "html"],
    *,
    all_results: bool = False,
) -> Path:
    temporary: str | None = None
    try:
        content = render(report, format, all_results=all_results)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"scan-{report.run.id}.{format}"
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return target
    except Exception as exc:
        raise ApplicationError(
            ErrorCode.REPORT_ERROR,
            "Report render/write failed; saved scan unchanged. Retry report with the same ID.",
        ) from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
