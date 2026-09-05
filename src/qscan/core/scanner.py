"""Session-indexed breakout baseline; callers validate calendar continuity."""

import math
from statistics import fmean, pstdev

from qscan.domain.analysis import RankedCandidate, Reason, SymbolAnalysis, WindowAnalysis
from qscan.domain.models import CloseSeries, ScanContext, Stage
from qscan.domain.rules import RuleConfig, ScoreBand

COMPARISON_TOLERANCE = 1e-12


def at_least(value: float, threshold: float) -> bool:
    return value >= threshold - COMPARISON_TOLERANCE


def at_most(value: float, threshold: float) -> bool:
    return value <= threshold + COMPARISON_TOLERANCE


def band_score(value: float | None, bands: tuple[ScoreBand, ...], *, ascending: bool) -> int:
    if value is None:
        return 0
    return max(
        (
            b.points
            for b in bands
            if (at_least(value, b.threshold) if ascending else at_most(value, b.threshold))
        ),
        default=0,
    )


def classify_stage(distance: float, sma_distance: float, rules: RuleConfig) -> Stage:
    if not at_most(distance, rules.extended_resistance) or not at_most(
        sma_distance, rules.extended_sma20
    ):
        return Stage.EXTENDED
    if not at_most(distance, rules.breakout_buffer):
        return Stage.CLOSE_BREAK_ABOVE
    if at_least(distance, rules.near_resistance_floor):
        return Stage.NEAR_CLOSE_RESISTANCE
    return Stage.FORMING


def analyze_symbol(series: CloseSeries, rules: RuleConfig, context: ScanContext) -> SymbolAnalysis:
    """Analyze a validated, contiguous series already clipped to the target session."""
    if series.sessions[-1] > context.as_of_session:
        raise ValueError("Future prices must be clipped before analysis")
    base = dict(
        instrument_id=series.instrument_id, context=context, config_hash=rules.config_hash()
    )

    def unavailable(code: str) -> SymbolAnalysis:
        return SymbolAnalysis(
            **base, evaluation_status="DATA_UNAVAILABLE", reasons=(Reason(code=code),)
        )

    if series.sessions[-1] != context.as_of_session:
        return unavailable("STALE_DATA")
    if len(series.closes) < rules.minimum_history:
        return unavailable("INSUFFICIENT_HISTORY")
    if series.sessions[-2] != context.reference_session:
        return unavailable("MISSING_REQUIRED_SESSION")
    c = series.closes
    if any(len(set(c[i : i + 20])) == 1 for i in range(len(c) - 19)):
        return unavailable("SUSPICIOUS_FLAT_SERIES")

    features: dict[str, float | None] = {}
    warnings: list[Reason] = []
    for k in (21, 63, 126):
        features[f"return_{k}"] = c[-2] / c[-2 - k] - 1 if len(c) >= k + 2 else None
        if features[f"return_{k}"] is None:
            warnings.append(
                Reason(code="UNAVAILABLE_FEATURE", parameters={"feature": f"return_{k}"})
            )
    sma20 = fmean(c[-20:])
    sma50 = fmean(c[-50:])
    slope = sma20 / fmean(c[-25:-5]) - 1
    distance_sma = c[-1] / sma20 - 1
    # Twenty background returns and five recent returns end at u, excluding t.
    returns = [math.log(b) - math.log(a) for a, b in zip(c[-27:-2], c[-26:-1], strict=True)]
    denominator = pstdev(returns[:20])
    contraction = pstdev(returns[20:]) / denominator if denominator >= rules.epsilon else None
    if contraction is None:
        warnings.append(Reason(code="UNDEFINED_CONTRACTION"))
    recent_range = max(c[-6:-1]) / min(c[-6:-1]) - 1
    features.update(
        sma10=fmean(c[-10:]),
        sma20=sma20,
        sma50=sma50,
        sma20_slope_5=slope,
        distance_to_sma20=distance_sma,
        contraction_ratio=contraction,
        close_range_5=recent_range,
    )
    windows: list[WindowAnalysis] = []
    for n in rules.windows:
        if len(c) < n + rules.prior_lookback + 1:
            windows.append(
                WindowAnalysis(
                    window_sessions=n,
                    available=False,
                    reasons=(
                        Reason(code="INSUFFICIENT_WINDOW_HISTORY", parameters={"required": n + 64}),
                    ),
                )
            )
            continue
        w = c[-n - 1 : -1]
        prior = w[0] / min(c[-n - 64 : -n - 1]) - 1
        resistance, support = max(w), min(w)
        spread = resistance / support - 1
        net = w[-1] / w[0] - 1
        higher_lows = min(w[n // 2 :]) / min(w[: n // 2])
        distance = c[-1] / resistance - 1
        wf = dict(
            prior_move=prior,
            close_resistance=resistance,
            close_support=support,
            base_close_range=spread,
            base_net_return=net,
            higher_close_lows_ratio=higher_lows,
            distance_to_resistance=distance,
        )
        reasons = []
        r63, r126 = features["return_63"], features["return_126"]
        if not (
            (r63 is not None and at_least(r63, rules.momentum_return_63))
            or (r126 is not None and at_least(r126, rules.momentum_return_126))
            or at_least(prior, rules.momentum_prior_move)
        ):
            reasons.append(Reason(code="MOMENTUM_GATE_FAILED"))
        if not at_most(spread, rules.max_base_range):
            reasons.append(Reason(code="BASE_RANGE_GATE_FAILED"))
        if not at_most(abs(net), rules.max_base_net_return):
            reasons.append(Reason(code="BASE_NET_RETURN_GATE_FAILED"))
        if not at_least(c[-1] / support, rules.support_floor):
            reasons.append(Reason(code="STRUCTURE_BROKEN"))
        if reasons:
            windows.append(
                WindowAnalysis(
                    window_sessions=n, available=True, features=wf, reasons=tuple(reasons)
                )
            )
            continue
        score = {
            "momentum": max(
                band_score(r63, rules.return_63_bands, ascending=True),
                band_score(r126, rules.return_126_bands, ascending=True),
                band_score(prior, rules.prior_move_bands, ascending=True),
            ),
            "trend": rules.trend_points
            * sum((at_least(c[-1] / sma20, 1), at_least(c[-1] / sma50, 1), not at_most(slope, 0))),
            "structure": band_score(spread, rules.base_range_bands, ascending=False)
            + band_score(abs(net), rules.base_net_bands, ascending=False)
            + band_score(higher_lows, rules.higher_lows_bands, ascending=True),
            "contraction": band_score(contraction, rules.contraction_bands, ascending=False)
            + band_score(recent_range, rules.close_range_bands, ascending=False),
            "proximity": band_score(abs(distance), rules.proximity_bands, ascending=False),
        }
        stage = classify_stage(distance, distance_sma, rules)
        windows.append(
            WindowAnalysis(
                window_sessions=n,
                available=True,
                eligible=True,
                features=wf,
                score_breakdown=score,
                score=sum(score.values()),
                stage=stage,
                is_close_break=not at_most(distance, rules.breakout_buffer),
                reasons=(
                    Reason(
                        code=stage.value,
                        parameters={
                            "distance": distance,
                            "reference_close": resistance,
                            "window_sessions": n,
                        },
                    ),
                ),
            )
        )
    selected = select_window(windows, rules)
    return SymbolAnalysis(
        **base,
        evaluation_status="EVALUATED",
        is_candidate=selected is not None,
        stage=selected.stage if selected else None,
        score=selected.score if selected else None,
        selected_window=selected,
        alternative_windows=tuple(w for w in windows if w is not selected),
        features=features,
        reasons=tuple(warnings),
    )


def select_window(windows: list[WindowAnalysis], rules: RuleConfig) -> WindowAnalysis | None:
    """Prefer a usable setup over an extended one before comparing scores."""
    return min(
        (w for w in windows if w.eligible),
        key=lambda w: (
            w.stage == Stage.EXTENDED,
            -(w.score or 0),
            rules.window_preference.index(w.window_sessions),
        ),
        default=None,
    )


def rank_candidates(analyses: list[SymbolAnalysis], rules: RuleConfig) -> list[RankedCandidate]:
    """Rank all eligible symbols, rejecting mixed rules and duplicate identities."""
    if any(a.config_hash != rules.config_hash() for a in analyses):
        raise ValueError("Cannot rank analyses from different rules")
    if len({a.instrument_id for a in analyses}) != len(analyses):
        raise ValueError("Duplicate instrument IDs")
    ordered = sorted(
        (a for a in analyses if a.is_candidate), key=lambda a: (-(a.score or 0), a.instrument_id)
    )
    return [RankedCandidate(rank=i, analysis=a) for i, a in enumerate(ordered, 1)]
