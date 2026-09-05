import math
from datetime import date, timedelta
from statistics import fmean, pstdev
from uuid import UUID

import pytest

from qscan.core import analyze_symbol, rank_candidates
from qscan.core.scanner import band_score, classify_stage, select_window
from qscan.domain.analysis import SymbolAnalysis, WindowAnalysis
from qscan.domain.models import CloseSeries, ScanContext, Stage
from qscan.domain.rules import RuleConfig

RULES = RuleConfig()


def make_series(closes, identity=1):
    # Synthetic session labels; exchange calendar validation belongs upstream.
    return CloseSeries(
        instrument_id=UUID(int=identity),
        closes=closes,
        sessions=tuple(date(2025, 1, 1) + timedelta(days=i) for i in range(len(closes))),
    )


def analyze(closes, identity=1, rules=RULES):
    series = make_series(closes, identity)
    context = ScanContext(
        as_of_session=series.sessions[-1],
        reference_session=series.sessions[-2],
        engine_version="0.1.0",
    )
    return analyze_symbol(series, rules, context)


def setup_prices():
    return [50 + i * 0.5 for i in range(88)] + [99, 100] * 20 + [100]


def all_windows(result):
    return {
        w.window_sessions: w
        for w in (
            ([result.selected_window] if result.selected_window else [])
            + list(result.alternative_windows)
        )
    }


def test_hand_calculated_setup_and_roundtrip():
    result = analyze(setup_prices())
    assert result.is_candidate
    assert result.stage == Stage.NEAR_CLOSE_RESISTANCE
    assert result.selected_window.window_sessions == 20
    assert result.score == 86
    assert result.selected_window.score_breakdown == {
        "momentum": 18,
        "trend": 15,
        "structure": 25,
        "contraction": 13,
        "proximity": 15,
    }
    assert result.selected_window.features["close_resistance"] == 100
    assert result.selected_window.features["base_close_range"] == pytest.approx(1 / 99)
    assert SymbolAnalysis.model_validate_json(result.model_dump_json()) == result


def test_formulas_and_nonoverlapping_returns():
    c = setup_prices()
    result = analyze(c)
    for k in (21, 63, 126):
        assert result.features[f"return_{k}"] == pytest.approx(c[-2] / c[-k - 2] - 1)
    for n in (10, 20, 50):
        assert result.features[f"sma{n}"] == pytest.approx(fmean(c[-n:]))
    r = [math.log(c[i] / c[i - 1]) for i in range(len(c) - 26, len(c) - 1)]
    assert result.features["contraction_ratio"] == pytest.approx(pstdev(r[-5:]) / pstdev(r[:20]))
    for n, window in all_windows(result).items():
        start = len(c) - n - 1
        assert window.features["prior_move"] == pytest.approx(
            c[start] / min(c[start - 63 : start]) - 1
        )


@pytest.mark.parametrize(
    "length,available",
    [(79, []), (80, [10]), (83, [10]), (84, [10, 20]), (103, [10, 20]), (104, [10, 20, 40])],
)
def test_history_boundaries(length, available):
    result = analyze(setup_prices()[-length:])
    assert sorted(n for n, w in all_windows(result).items() if w.available) == available
    if length >= 80:
        assert result.features["return_126"] is None


@pytest.mark.parametrize(
    "distance,sma,stage",
    [
        (-0.05000001, 0, Stage.FORMING),
        (-0.05, 0, Stage.NEAR_CLOSE_RESISTANCE),
        (0.002, 0, Stage.NEAR_CLOSE_RESISTANCE),
        (0.00200001, 0, Stage.CLOSE_BREAK_ABOVE),
        (0.1, 0.2, Stage.CLOSE_BREAK_ABOVE),
        (0.10000001, 0, Stage.EXTENDED),
        (0, 0.20000001, Stage.EXTENDED),
        (100.2 / 100 - 1, 0, Stage.NEAR_CLOSE_RESISTANCE),
    ],
)
def test_stage_boundaries(distance, sma, stage):
    assert classify_stage(distance, sma, RULES) == stage


@pytest.mark.parametrize(
    "name", [name for name in RuleConfig.model_fields if name.endswith("_bands")]
)
def test_every_scoring_band_boundary(name):
    bands = getattr(RULES, name)
    ascending = name in {
        "return_63_bands",
        "return_126_bands",
        "prior_move_bands",
        "higher_lows_bands",
    }
    assert band_score(None, bands, ascending=ascending) == 0
    for band in bands:
        assert band_score(band.threshold, bands, ascending=ascending) == band.points
        outside = band.threshold + (-1e-8 if ascending else 1e-8)
        assert band_score(outside, bands, ascending=ascending) < band.points


def test_today_does_not_change_reference_features():
    c = setup_prices()
    before, after = analyze(c), analyze(c[:-1] + [120])
    for n, w in all_windows(before).items():
        other = all_windows(after)[n]
        for name, value in w.features.items():
            if name != "distance_to_resistance":
                assert other.features[name] == value
    for name in ("return_63", "return_126", "close_range_5", "contraction_ratio"):
        assert before.features[name] == after.features[name]
    assert after.stage == Stage.EXTENDED
    assert after.selected_window.is_close_break


def test_future_rejected_and_clipped_snapshot_invariant():
    c = setup_prices()
    baseline = analyze(c)
    with pytest.raises(ValueError, match="Future"):
        analyze_symbol(make_series(c + [1]), RULES, baseline.context)
    for future in ([1], [10000, 2]):
        assert analyze((c + future)[: len(c)]) == baseline


def test_data_errors_and_no_setup_are_distinct():
    assert analyze([100] * 100).reasons[0].code == "SUSPICIOUS_FLAT_SERIES"
    assert analyze([100 - i * 0.5 for i in range(100)]).evaluation_status == "EVALUATED"
    assert not analyze([100 - i * 0.5 for i in range(100)]).is_candidate
    broken = analyze(setup_prices()[:-1] + [80])
    assert not broken.is_candidate
    assert all(
        "STRUCTURE_BROKEN" in [r.code for r in w.reasons] for w in all_windows(broken).values()
    )
    baseline = analyze(setup_prices())
    stale = analyze_symbol(make_series(setup_prices()[:-1]), RULES, baseline.context)
    assert stale.reasons[0].code == "STALE_DATA"


def test_zero_volatility_denominator_is_unavailable_not_zero():
    result = analyze([100 * 1.001**i for i in range(100)])
    assert result.features["contraction_ratio"] is None
    assert "UNDEFINED_CONTRACTION" in [r.code for r in result.reasons]


def test_ranking_ties_and_rule_mismatch():
    one, two = analyze(setup_prices(), 1), analyze(setup_prices(), 2)
    assert [r.analysis.instrument_id.int for r in rank_candidates([two, one], RULES)] == [1, 2]
    assert [r.rank for r in rank_candidates([two, one], RULES)] == [1, 2]
    with pytest.raises(ValueError, match="different rules"):
        rank_candidates([one], RuleConfig(breakout_buffer=0.003))
    with pytest.raises(ValueError, match="Duplicate"):
        rank_candidates([one, one], RULES)


@pytest.mark.parametrize(
    "field,feature,reason",
    [
        ("max_base_range", "base_close_range", "BASE_RANGE_GATE_FAILED"),
        ("max_base_net_return", "base_net_return", "BASE_NET_RETURN_GATE_FAILED"),
    ],
)
def test_base_gate_thresholds(field, feature, reason):
    c = setup_prices()
    boundary = abs(all_windows(analyze(c))[20].features[feature])
    for offset, eligible in [(0, True), (-1e-8, False)]:
        rules = RuleConfig.model_validate({field: boundary + offset})
        window = all_windows(analyze(c, rules=rules))[20]
        assert window.eligible == eligible
        assert (reason in [r.code for r in window.reasons]) != eligible


@pytest.mark.parametrize(
    "field,feature",
    [
        ("momentum_return_63", "return_63"),
        ("momentum_return_126", "return_126"),
        ("momentum_prior_move", "prior_move"),
    ],
)
def test_each_momentum_or_branch_at_boundary(field, feature):
    c = setup_prices()
    baseline = analyze(c)
    boundary = (
        all_windows(baseline)[20].features[feature]
        if feature == "prior_move"
        else baseline.features[feature]
    )
    for offset, eligible in [(0, True), (1e-8, False)]:
        config = dict(momentum_return_63=100, momentum_return_126=100, momentum_prior_move=100)
        config[field] = boundary + offset
        window = all_windows(analyze(c, rules=RuleConfig.model_validate(config)))[20]
        assert window.eligible == eligible


def test_support_floor_boundary():
    c = setup_prices()
    support = all_windows(analyze(c))[20].features["close_support"]
    assert all_windows(analyze(c[:-1] + [support * 0.95]))[20].eligible
    assert not all_windows(analyze(c[:-1] + [support * (0.95 - 1e-8)]))[20].eligible


def test_window_selection_precedence_and_all_extended():
    def window(n, score, stage, eligible=True):
        return WindowAnalysis(
            window_sessions=n, available=True, eligible=eligible, score=score, stage=stage
        )

    near = window(10, 20, Stage.NEAR_CLOSE_RESISTANCE)
    extended = window(20, 100, Stage.EXTENDED)
    assert select_window([extended, near], RULES) == near
    for order in ([40, 10, 20], [20, 40, 10]):
        assert (
            select_window([window(n, 80, Stage.EXTENDED) for n in order], RULES).window_sessions
            == 20
        )
    assert select_window([window(10, 90, Stage.EXTENDED), extended], RULES) == extended
    assert select_window([window(20, 100, Stage.FORMING, False)], RULES) is None


def test_short_history_can_pass_without_return_126():
    c = [50 + i * 0.7 for i in range(69)] + [99, 100] * 5 + [100]
    result = analyze(c)
    assert result.is_candidate
    assert result.features["return_126"] is None
    assert result.selected_window.window_sessions == 10


def test_low_range_without_momentum_and_straight_rise():
    assert not analyze([99, 100] * 65).is_candidate
    assert not analyze([100 * 1.03**i for i in range(130)]).is_candidate


def test_reference_mismatch_is_data_error():
    c = setup_prices()
    context = analyze(c).context.model_copy(update={"reference_session": date(2025, 1, 2)})
    result = analyze_symbol(make_series(c), RULES, context)
    assert result.reasons[0].code == "MISSING_REQUIRED_SESSION"


def test_core_import_boundary():
    import ast
    import inspect

    import qscan.core.scanner as scanner

    tree = ast.parse(inspect.getsource(scanner))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module)
    assert set(imports) <= {
        "math",
        "statistics",
        "qscan.domain.analysis",
        "qscan.domain.models",
        "qscan.domain.rules",
    }
