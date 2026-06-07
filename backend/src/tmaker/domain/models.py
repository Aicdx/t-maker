from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class SignalKind(StrEnum):
    CANDIDATE_BUY = "candidate_buy"
    CANDIDATE_SELL = "candidate_sell"
    SUSPECTED = "suspected"
    HOLD = "hold"


class SignalAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Candle(BaseModel):
    symbol: str
    timestamp: datetime
    open: float = Field(ge=0)
    high: float = Field(ge=0)
    low: float = Field(ge=0)
    close: float = Field(ge=0)
    volume: float = Field(ge=0)


class MarketQuote(BaseModel):
    symbol: str
    name: str
    latest: float = Field(ge=0)
    previous_close: float = Field(ge=0)
    open: float = Field(ge=0)
    high: float = Field(ge=0)
    low: float = Field(ge=0)
    change: float
    change_percent: float


class Position(BaseModel):
    symbol: str
    base_quantity: int = Field(ge=0)
    cost_price: float = Field(ge=0)
    available_cash: float = Field(ge=0)
    t_quantity: int = Field(ge=0)


class ProviderHealth(BaseModel):
    provider: str
    symbol: str
    last_success_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    stale_after_seconds: int = Field(default=90, gt=0)
    missing_candle_count: int = Field(default=0, ge=0)
    last_error: str | None = None

    def is_stale(self, now: datetime) -> bool:
        if self.last_success_at is None:
            return True
        return (now - self.last_success_at).total_seconds() > self.stale_after_seconds

    def status_at(self, now: datetime) -> str:
        return "data_delayed" if self.is_stale(now) else "ok"


class LlmReview(BaseModel):
    action: SignalAction
    confidence: float = Field(ge=0, le=1)
    summary: str
    reasons: list[str]
    risks: list[str]
    wait_for: list[str]
    execution_allowed: bool = True
    execution_blockers: list[str] = Field(default_factory=list)


class Signal(BaseModel):
    symbol: str
    timestamp: datetime
    kind: SignalKind
    action: SignalAction
    confidence: float = Field(ge=0, le=1)
    rule_ids: list[str]
    reason: str
    risks: list[str]
    source_fresh: bool
    llm_review: LlmReview | None = None
    llm_status: str = "not_requested"

    @computed_field
    @property
    def needs_llm_review(self) -> bool:
        return self.kind in {
            SignalKind.CANDIDATE_BUY,
            SignalKind.CANDIDATE_SELL,
            SignalKind.SUSPECTED,
        }
