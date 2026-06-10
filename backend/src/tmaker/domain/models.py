from __future__ import annotations

from datetime import date
from datetime import datetime
from enum import StrEnum
from itertools import groupby

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


class TradeConfirmationAction(StrEnum):
    BUY = "buy"
    SELL = "sell"


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


class TradeConfirmationCreate(BaseModel):
    symbol: str
    signal_timestamp: datetime
    signal_action: SignalAction
    confirm_action: TradeConfirmationAction
    price: float = Field(ge=0)
    quantity: int = Field(default=100, gt=0)
    source: str
    reason: str
    llm_confidence: float | None = Field(default=None, ge=0, le=1)


class TradeConfirmation(TradeConfirmationCreate):
    id: str
    trade_date: date
    created_at: datetime


class TradeConfirmationPair(BaseModel):
    symbol: str
    buy_id: str
    sell_id: str
    buy_price: float
    sell_price: float
    quantity: int
    spread: float
    pnl: float
    opened_at: datetime
    closed_at: datetime


class TradeConfirmationSummary(BaseModel):
    record_count: int
    paired_count: int
    unpaired_count: int
    total_pnl: float


class TradeConfirmationStats(BaseModel):
    date: date
    quantity_per_trade: int = 100
    summary: TradeConfirmationSummary
    pairs: list[TradeConfirmationPair]
    unpaired: list[TradeConfirmation]


def build_trade_confirmation_stats(
    confirmations: list[TradeConfirmation],
    trade_date: date,
) -> TradeConfirmationStats:
    scoped = [item for item in confirmations if item.trade_date == trade_date]
    sorted_items = sorted(scoped, key=lambda item: (item.symbol, item.signal_timestamp, item.created_at))
    pairs: list[TradeConfirmationPair] = []
    unpaired: list[TradeConfirmation] = []

    for symbol, symbol_items_iter in groupby(sorted_items, key=lambda item: item.symbol):
        pending_buys: list[TradeConfirmation] = []
        pending_sells: list[TradeConfirmation] = []
        for item in symbol_items_iter:
            if item.confirm_action == TradeConfirmationAction.BUY:
                if pending_sells:
                    sell = pending_sells.pop(0)
                    pairs.append(_confirmation_pair(symbol, buy=item, sell=sell))
                else:
                    pending_buys.append(item)
            else:
                if pending_buys:
                    buy = pending_buys.pop(0)
                    pairs.append(_confirmation_pair(symbol, buy=buy, sell=item))
                else:
                    pending_sells.append(item)
        unpaired.extend(pending_buys)
        unpaired.extend(pending_sells)

    total_pnl = round(sum(pair.pnl for pair in pairs), 2)
    return TradeConfirmationStats(
        date=trade_date,
        summary=TradeConfirmationSummary(
            record_count=len(scoped),
            paired_count=len(pairs),
            unpaired_count=len(unpaired),
            total_pnl=total_pnl,
        ),
        pairs=pairs,
        unpaired=sorted(unpaired, key=lambda item: (item.signal_timestamp, item.created_at)),
    )


def _confirmation_pair(
    symbol: str,
    *,
    buy: TradeConfirmation,
    sell: TradeConfirmation,
) -> TradeConfirmationPair:
    quantity = min(buy.quantity, sell.quantity)
    spread = round(sell.price - buy.price, 4)
    pnl = round(spread * quantity, 2)
    opened_at = min(buy.signal_timestamp, sell.signal_timestamp)
    closed_at = max(buy.signal_timestamp, sell.signal_timestamp)
    return TradeConfirmationPair(
        symbol=symbol,
        buy_id=buy.id,
        sell_id=sell.id,
        buy_price=buy.price,
        sell_price=sell.price,
        quantity=quantity,
        spread=spread,
        pnl=pnl,
        opened_at=opened_at,
        closed_at=closed_at,
    )
