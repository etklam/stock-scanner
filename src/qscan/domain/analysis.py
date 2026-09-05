"""Serializable outputs shared by all scanner entry points."""

from typing import Literal
from uuid import UUID

from pydantic import FiniteFloat

from qscan.domain.models import Contract, ScanContext, Stage


class Reason(Contract):
    code: str
    parameters: dict[str, FiniteFloat | str | int] = {}


class WindowAnalysis(Contract):
    window_sessions: int
    available: bool
    eligible: bool = False
    features: dict[str, FiniteFloat | None] = {}
    score_breakdown: dict[str, int] = {}
    score: int | None = None
    stage: Stage | None = None
    is_close_break: bool = False
    reasons: tuple[Reason, ...] = ()


class SymbolAnalysis(Contract):
    instrument_id: UUID
    context: ScanContext
    config_hash: str
    evaluation_status: Literal["EVALUATED", "DATA_UNAVAILABLE"]
    is_candidate: bool = False
    stage: Stage | None = None
    score: int | None = None
    selected_window: WindowAnalysis | None = None
    alternative_windows: tuple[WindowAnalysis, ...] = ()
    features: dict[str, FiniteFloat | None] = {}
    reasons: tuple[Reason, ...] = ()
    liquidity: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"


class RankedCandidate(Contract):
    rank: int
    analysis: SymbolAnalysis
