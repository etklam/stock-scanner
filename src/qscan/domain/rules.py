"""Versioned rule configuration with deterministic serialization."""

import hashlib
import json
from typing import Literal, Self

from pydantic import Field, FiniteFloat, model_validator

from qscan.domain.models import Contract


class ScoreBand(Contract):
    threshold: FiniteFloat
    points: int = Field(ge=0, le=25)


def bands(*values: tuple[float, int]) -> tuple[ScoreBand, ...]:
    return tuple(ScoreBand(threshold=value, points=points) for value, points in values)


class RuleConfig(Contract):
    ruleset_id: Literal["breakout-v1"] = "breakout-v1"
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    windows: tuple[Literal[10, 20, 40], ...] = (10, 20, 40)
    window_preference: tuple[Literal[10, 20, 40], ...] = (20, 10, 40)
    minimum_history: int = Field(default=80, ge=80)
    prior_lookback: Literal[63] = 63
    epsilon: float = Field(default=1e-12, gt=0, le=1e-9)
    momentum_return_63: float = Field(default=0.20, ge=0)
    momentum_return_126: float = Field(default=0.30, ge=0)
    momentum_prior_move: float = Field(default=0.25, ge=0)
    max_base_range: float = Field(default=0.25, ge=0)
    max_base_net_return: float = Field(default=0.15, ge=0)
    support_floor: float = Field(default=0.95, gt=0, le=1)
    extended_resistance: float = Field(default=0.10, gt=0)
    extended_sma20: float = Field(default=0.20, gt=0)
    breakout_buffer: float = Field(default=0.002, ge=0)
    near_resistance_floor: float = Field(default=-0.05, le=0)
    trend_points: int = Field(default=5, ge=0, le=5)
    return_63_bands: tuple[ScoreBand, ...] = bands((0.20, 10), (0.40, 18), (0.60, 25))
    return_126_bands: tuple[ScoreBand, ...] = bands((0.30, 10), (0.60, 18), (1.00, 25))
    prior_move_bands: tuple[ScoreBand, ...] = bands((0.25, 10), (0.50, 18), (0.75, 25))
    base_range_bands: tuple[ScoreBand, ...] = bands((0.08, 15), (0.15, 10), (0.25, 5))
    base_net_bands: tuple[ScoreBand, ...] = bands((0.05, 5), (0.10, 3), (0.15, 1))
    higher_lows_bands: tuple[ScoreBand, ...] = bands((0.95, 2), (1.00, 5))
    contraction_bands: tuple[ScoreBand, ...] = bands((0.60, 10), (0.80, 7), (1.00, 3))
    close_range_bands: tuple[ScoreBand, ...] = bands((0.03, 10), (0.05, 7), (0.08, 3))
    proximity_bands: tuple[ScoreBand, ...] = bands((0.02, 15), (0.05, 10), (0.10, 5))

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        if not self.windows or len(set(self.windows)) != len(self.windows):
            raise ValueError("Windows must be nonempty and unique")
        if set(self.window_preference) != set(self.windows) or len(self.window_preference) != len(
            self.windows
        ):
            raise ValueError("Window preference must be a permutation of windows")
        if self.breakout_buffer >= self.extended_resistance:
            raise ValueError("Breakout buffer must be below extended resistance")
        caps = {
            "return_63_bands": 25,
            "return_126_bands": 25,
            "prior_move_bands": 25,
            "base_range_bands": 15,
            "base_net_bands": 5,
            "higher_lows_bands": 5,
            "contraction_bands": 10,
            "close_range_bands": 10,
            "proximity_bands": 15,
        }
        ascending = {"return_63_bands", "return_126_bands", "prior_move_bands", "higher_lows_bands"}
        for name, cap in caps.items():
            values: tuple[ScoreBand, ...] = getattr(self, name)
            if not values or any(v.points > cap or v.threshold < 0 for v in values):
                raise ValueError(f"Invalid scoring bands: {name}")
            for left, right in zip(values, values[1:], strict=False):
                if left.threshold >= right.threshold:
                    raise ValueError(f"Thresholds must increase: {name}")
                if (right.points > left.points) != (name in ascending):
                    raise ValueError(f"Points must follow threshold direction: {name}")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
