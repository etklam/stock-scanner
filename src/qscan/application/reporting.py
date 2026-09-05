"""Immutable run comparisons and transport-neutral report/chart data."""

import math
from datetime import date
from uuid import UUID

from pydantic import FiniteFloat

from qscan.application.contracts import (
    ApplicationError,
    Change,
    ChangeCode,
    Comparison,
    InputItem,
    InputSnapshot,
    Repository,
    Run,
    ScanResult,
    Snapshots,
)
from qscan.domain.analysis import Reason
from qscan.domain.models import Contract, ErrorCode, RunState


class ComparisonService:
    def __init__(self, repository: Repository, snapshots: Snapshots) -> None:
        self.repository, self.snapshots = repository, snapshots

    def bind(self, run: Run) -> Comparison:
        previous = run.context.reference_session
        eligible = []
        rejected: set[str] = set()
        for old in self.repository.summaries(None):
            if old.watchlist.id != run.watchlist.id or old.context.as_of_session != previous:
                continue
            if old.source_run_id is not None or old.finished_at is None:
                continue
            if old.finished_at > run.started_at:
                continue
            reasons = self.incompatibilities(run, old)
            if reasons:
                rejected.update(reasons)
            elif old.state in (RunState.SUCCEEDED, RunState.PARTIAL) and old.counts.evaluated:
                eligible.append(old)
        baseline = max(eligible, key=lambda r: (r.finished_at, str(r.id))) if eligible else None
        reasons = () if baseline else tuple(sorted(rejected)) or ("NO_EXACT_PREVIOUS_SESSION",)
        return Comparison(
            previous_run_id=baseline.id if baseline else None,
            previous_session=previous,
            current_session=run.context.as_of_session,
            reasons=reasons,
            changes=()
            if baseline
            else (
                Change(
                    codes=("COMPARISON_UNAVAILABLE" if rejected else "NO_BASELINE",),
                    reasons=reasons,
                ),
            ),
        )

    @staticmethod
    def incompatibilities(run: Run, old: Run) -> tuple[str, ...]:
        checks = {
            "WATCHLIST_MISMATCH": run.watchlist.id != old.watchlist.id,
            "CONFIG_MISMATCH": run.config_hash != old.config_hash,
            "BASIS_MISMATCH": run.price_basis != old.price_basis,
            "ENGINE_MISMATCH": run.context.engine_version != old.context.engine_version,
            "SESSION_MISMATCH": run.context.reference_session != old.context.as_of_session,
        }
        return tuple(k for k, mismatch in checks.items() if mismatch)

    def get(self, identity: UUID) -> Comparison:
        run = self.repository.run(identity)
        return run.comparison or self.unavailable(run)

    @staticmethod
    def unavailable(run: Run) -> Comparison:
        replay = run.source_run_id is not None
        reason = "REPLAY_NOT_DAILY_SCAN" if replay else "LEGACY_BASELINE_NOT_RECORDED"
        return Comparison(
            previous_session=run.context.reference_session,
            current_session=run.context.as_of_session,
            binding="replay_unavailable" if replay else "legacy_unavailable",
            reasons=(reason,),
            changes=(Change(codes=("COMPARISON_UNAVAILABLE",), reasons=(reason,)),),
        )

    def complete(self, run: Run, snapshot: InputSnapshot) -> Comparison:
        binding = run.comparison or self.unavailable(run)
        if binding.previous_run_id is None:
            return binding
        old = self.repository.run(binding.previous_run_id)
        reasons = self.incompatibilities(run, old)
        if reasons:
            return binding.model_copy(
                update={
                    "reasons": reasons,
                    "changes": (Change(codes=("COMPARISON_UNAVAILABLE",), reasons=reasons),),
                }
            )
        try:
            if old.input_hash is None:
                raise ApplicationError(ErrorCode.SCAN_FAILED)
            prior = self.snapshots.read(old.input_hash)
            if prior.context != old.context or prior.watchlist != old.watchlist:
                raise ApplicationError(ErrorCode.SCAN_FAILED)
        except ApplicationError:
            return binding.model_copy(
                update={
                    "reasons": ("BASELINE_SNAPSHOT_UNAVAILABLE",),
                    "changes": (
                        Change(
                            codes=("COMPARISON_UNAVAILABLE",),
                            reasons=("BASELINE_SNAPSHOT_UNAVAILABLE",),
                        ),
                    ),
                }
            )
        before = {r.instrument.id: r for r in old.results}
        after = {r.instrument.id: r for r in run.results}
        old_items = {i.instrument.id: i for i in prior.items}
        new_items = {i.instrument.id: i for i in snapshot.items}
        changes = []
        for identity in sorted(before.keys() | after.keys()):
            a, b = before.get(identity), after.get(identity)
            codes: list[ChangeCode] = []
            why: tuple[str, ...] = ()
            if a is None or b is None:
                codes.append("UNIVERSE_CHANGED")
                why = ("ADDED" if a is None else "REMOVED",)
            elif a.category != "evaluated" or b.category != "evaluated":
                codes.append("COMPARISON_UNAVAILABLE")
                why = ("BOTH_DAYS_REQUIRE_VALID_EVALUATION",)
            else:
                ai, bi = old_items[identity], new_items[identity]
                if (
                    ai.provenance is None
                    or bi.provenance is None
                    or ai.provenance.provider != bi.provenance.provider
                ):
                    codes.append("COMPARISON_UNAVAILABLE")
                    why = ("SOURCE_MISMATCH",)
                else:
                    x, y = a.analysis, b.analysis
                    if x.is_candidate != y.is_candidate:
                        codes.append("NEW_CANDIDATE" if y.is_candidate else "DROPPED_CANDIDATE")
                    if x.stage != y.stage:
                        codes.append("STAGE_CHANGED")
                    if x.score != y.score:
                        codes.append("SCORE_CHANGED")
                    if (x.selected_window.window_sessions if x.selected_window else None) != (
                        y.selected_window.window_sessions if y.selected_window else None
                    ):
                        codes.append("WINDOW_CHANGED")
                    assert ai.series is not None and bi.series is not None
                    prices = dict(zip(ai.series.sessions, ai.series.closes, strict=True))
                    if any(
                        s in prices and not math.isclose(c, prices[s], rel_tol=1e-8, abs_tol=1e-10)
                        for s, c in zip(bi.series.sessions, bi.series.closes, strict=True)
                    ):
                        codes.append("DATA_REVISION_DIFF")
            if codes:
                result = b or a
                assert result is not None
                changes.append(
                    Change(
                        instrument_id=identity,
                        symbol=result.instrument.display_symbol,
                        codes=tuple(codes),
                        reasons=why,
                        previous=a.analysis if a else None,
                        current=b.analysis if b else None,
                    )
                )
        return binding.model_copy(update={"changes": tuple(changes)})


class ChartSeries(Contract):
    instrument_id: UUID
    symbol: str
    sessions: tuple[date, ...]
    closes: tuple[FiniteFloat, ...]
    sma: dict[str, tuple[FiniteFloat | None, ...]]
    window_start: date | None = None
    window_end: date | None = None
    close_resistance: FiniteFloat | None = None


class WindowReasons(Contract):
    window_sessions: int
    available: bool
    eligible: bool
    reasons: tuple[Reason, ...]


class ResultReasons(Contract):
    symbol_reasons: tuple[Reason, ...]
    symbol_warnings: tuple[str, ...]
    selected_window_reasons: tuple[Reason, ...]
    windows: tuple[WindowReasons, ...]


def reason_details(result: ScanResult) -> ResultReasons:
    analysis = result.analysis
    selected = analysis.selected_window
    windows = ((selected,) if selected else ()) + analysis.alternative_windows
    return ResultReasons(
        symbol_reasons=analysis.reasons,
        symbol_warnings=(
            *result.warnings,
            *(result.provenance.warnings if result.provenance else ()),
        ),
        selected_window_reasons=selected.reasons if selected else (),
        windows=tuple(
            WindowReasons(
                window_sessions=w.window_sessions,
                available=w.available,
                eligible=w.eligible,
                reasons=w.reasons,
            )
            for w in windows
        ),
    )


class Report(Contract):
    schema_version: int = 1
    run: Run
    comparison: Comparison
    sources: tuple[str, ...]
    warnings: tuple[str, ...]
    charts: tuple[ChartSeries, ...] = ()
    chart_error: str | None = None
    charts_included: bool = True
    explanations: dict[str, ResultReasons] = {}
    displayed_candidates: int
    limitation: str = "close-only 初篩，流動性及日內形態未評估"


class ReportService:
    def __init__(self, repository: Repository, snapshots: Snapshots) -> None:
        self.repository, self.snapshots = repository, snapshots

    def series(self, identity: UUID, instrument_id: UUID) -> ChartSeries:
        run = self.repository.run(identity)
        snapshot = self.load_snapshot(run)
        return self.chart_data(run, snapshot, (instrument_id,))[0]

    def load_snapshot(self, run: Run) -> InputSnapshot:
        if run.input_hash is None:
            raise ApplicationError(ErrorCode.SCAN_NOT_READY, "Run has no input snapshot")
        snapshot = self.snapshots.read(run.input_hash)
        if (
            snapshot.context != run.context
            or snapshot.watchlist != run.watchlist
            or snapshot.rules.config_hash() != run.config_hash
        ):
            raise ApplicationError(ErrorCode.SCAN_FAILED, "Snapshot does not match run")
        return snapshot

    def chart_data(
        self, run: Run, snapshot: InputSnapshot, identities: tuple[UUID, ...]
    ) -> tuple[ChartSeries, ...]:
        results = {r.instrument.id: r for r in run.results}
        items = {i.instrument.id: i for i in snapshot.items}
        return tuple(self._chart(run, results.get(i), items.get(i)) for i in identities)

    @staticmethod
    def _chart(run: Run, result: ScanResult | None, item: InputItem | None) -> ChartSeries:
        if result is None or item is None:
            raise ApplicationError(ErrorCode.NOT_FOUND)
        if item.series is None:
            raise ApplicationError(ErrorCode.NO_DATA, "Run has no valid series for instrument")
        series = item.series
        sma = {
            str(n): tuple(
                sum(series.closes[i - n + 1 : i + 1]) / n if i + 1 >= n else None
                for i in range(len(series.closes))
            )
            for n in (10, 20, 50)
        }
        window = result.analysis.selected_window
        end = series.sessions.index(run.context.reference_session)
        return ChartSeries(
            instrument_id=item.instrument.id,
            symbol=item.instrument.display_symbol,
            sessions=series.sessions,
            closes=series.closes,
            sma=sma,
            window_start=series.sessions[end - window.window_sessions + 1] if window else None,
            window_end=run.context.reference_session if window else None,
            close_resistance=window.features.get("close_resistance") if window else None,
        )

    def build(self, identity: UUID, top: int = 30, *, include_charts: bool = True) -> Report:
        if not 1 <= top <= 50:
            raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Top must be 1..50")
        run = self.repository.run(identity)
        displayed = [r for r in run.results if r.rank is not None][:top]
        charts: tuple[ChartSeries, ...] = ()
        error = None
        if include_charts and displayed:
            try:
                snapshot = self.load_snapshot(run)
                charts = self.chart_data(run, snapshot, tuple(r.instrument.id for r in displayed))
            except ApplicationError:
                error = "SNAPSHOT_UNAVAILABLE: chart omitted; no cache fallback"
        if run.state not in (RunState.SUCCEEDED, RunState.PARTIAL):
            error = f"RUN_{run.state.value}: no successful scan claimed"
        warnings: set[str] = set()
        sources = set()
        for result in run.results:
            warnings.update(result.warnings)
            if result.provenance:
                sources.add(result.provenance.provider)
                warnings.update(result.provenance.warnings)
                warnings.update(result.provenance.revisions)
        if "fixture" in sources:
            warnings.add("SYNTHETIC / DEMO — not real market data")
        return Report(
            run=run,
            comparison=run.comparison or ComparisonService.unavailable(run),
            sources=tuple(sorted(sources)),
            warnings=tuple(sorted(warnings)),
            charts=tuple(charts),
            chart_error=error,
            displayed_candidates=len(displayed),
            charts_included=include_charts,
            explanations={str(r.instrument.id): reason_details(r) for r in run.results},
        )
