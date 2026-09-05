from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from qscan.domain.models import CloseSeries, ScanContext
from qscan.domain.rules import RuleConfig
from qscan.interfaces.api.schemas import CreateScan

INSTRUMENT = UUID("00000000-0000-0000-0000-000000000001")


def series(**changes):
    return CloseSeries.model_validate(
        {
            "instrument_id": INSTRUMENT,
            "sessions": [date(2026, 9, 3), date(2026, 9, 4)],
            "closes": [100.0, 101.0],
            **changes,
        }
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 0, -1])
def test_invalid_prices_rejected(bad):
    with pytest.raises(ValidationError):
        series(closes=[100, bad])


@pytest.mark.parametrize(
    "changes",
    [
        {"closes": [100]},
        {"sessions": [], "closes": []},
        {"sessions": ["2026-09-04", "2026-09-03"]},
        {"sessions": ["2026-09-04", "2026-09-04"]},
        {"volume": [10, 20]},
        {"high": [101, 102]},
        {"price_basis": "adjusted_close"},
    ],
)
def test_bad_shape_and_non_close_inputs_rejected(changes):
    with pytest.raises(ValidationError):
        series(**changes)


def test_series_roundtrip_and_immutability():
    value = series()
    assert CloseSeries.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        value.closes = (1, 2)


def test_context_reference_must_precede_target():
    with pytest.raises(ValidationError):
        ScanContext(
            as_of_session=date(2026, 9, 4),
            reference_session=date(2026, 9, 4),
            engine_version="0.1.0",
        )


def test_config_hash_is_canonical_and_covers_changes():
    rules = RuleConfig()
    reversed_fields = dict(reversed(list(rules.model_dump().items())))
    assert RuleConfig.model_validate(reversed_fields).config_hash() == rules.config_hash()
    assert RuleConfig(breakout_buffer=0.003).config_hash() != rules.config_hash()
    assert len(rules.config_hash()) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"windows": [10, 10]},
        {"window_preference": [10, 20]},
        {"breakout_buffer": 0.11},
        {"epsilon": 0},
        {"momentum_return_63": float("nan")},
        {"expression": "1 + 1"},
        {"base_range_bands": [{"threshold": 0.08, "points": 25}]},
        {"return_63_bands": [{"threshold": 0.4, "points": 18}, {"threshold": 0.2, "points": 25}]},
    ],
)
def test_invalid_rules_rejected(changes):
    with pytest.raises(ValidationError):
        RuleConfig.model_validate(changes)


@pytest.mark.parametrize(
    "extra",
    [
        {"owner_id": "other"},
        {"output": "../../x"},
        {"ruleset_id": "arbitrary"},
        {"data_mode": "download"},
    ],
)
def test_scan_contract_rejects_client_authority_and_unknown_values(extra):
    with pytest.raises(ValidationError):
        CreateScan.model_validate({"watchlist_id": str(INSTRUMENT), **extra})


def test_scan_json_date_and_defaults():
    request = CreateScan.model_validate_json(
        '{"watchlist_id":"00000000-0000-0000-0000-000000000001","as_of_session":"2026-09-04"}'
    )
    assert request.as_of_session == date(2026, 9, 4)
    assert request.model_dump(mode="json")["data_mode"] == "auto"
