from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from typing import Literal, Sequence


TradeAction = Literal["buy", "sell"]


@dataclass(frozen=True)
class TradeActionPoint:
    id: str
    symbol: str
    timestamp: datetime
    action: TradeAction
    price: float


@dataclass(frozen=True)
class TTradePair:
    symbol: str
    buy: TradeActionPoint
    sell: TradeActionPoint
    spread: float
    spread_pct: float

    @property
    def order(self) -> str:
        if self.buy.timestamp <= self.sell.timestamp:
            return "buy->sell"
        return "sell->buy"


@dataclass(frozen=True)
class TPairingResult:
    pairs: list[TTradePair]
    unpaired: list[TradeActionPoint]


def select_open_reprice_pairs(
    points: Sequence[TradeActionPoint],
    *,
    max_actions: int | None = None,
    max_pairs: int | None = None,
    require_directional_close: bool = False,
) -> TPairingResult:
    pairs: list[TTradePair] = []
    unpaired: list[TradeActionPoint] = []

    sorted_points = sorted(points, key=lambda item: (item.symbol, item.timestamp, item.id))
    for symbol, symbol_points_iter in groupby(sorted_points, key=lambda item: item.symbol):
        symbol_pairs, symbol_unpaired = _select_symbol_open_reprice_pairs(
            list(symbol_points_iter),
            symbol=symbol,
            max_actions=max_actions,
            max_pairs=max_pairs,
            require_directional_close=require_directional_close,
        )
        pairs.extend(symbol_pairs)
        unpaired.extend(symbol_unpaired)
    return TPairingResult(
        pairs=sorted(pairs, key=lambda item: (item.symbol, min(item.buy.timestamp, item.sell.timestamp))),
        unpaired=sorted(unpaired, key=lambda item: (item.symbol, item.timestamp, item.id)),
    )


class LiveTTradePlanner:
    def __init__(
        self,
        *,
        max_actions_per_symbol_day: int = 4,
        max_pairs_per_symbol_day: int = 2,
    ) -> None:
        self.max_actions_per_symbol_day = max_actions_per_symbol_day
        self.max_pairs_per_symbol_day = max_pairs_per_symbol_day
        self._states: dict[tuple[str, str], _LiveSymbolState] = {}

    def should_notify(self, point: TradeActionPoint) -> bool:
        key = (point.symbol, point.timestamp.date().isoformat())
        state = self._states.setdefault(key, _LiveSymbolState())
        return state.should_notify(
            point,
            max_actions=self.max_actions_per_symbol_day,
            max_pairs=self.max_pairs_per_symbol_day,
        )


@dataclass
class _LiveSymbolState:
    pending_open: TradeActionPoint | None = None
    action_count: int = 0
    pair_count: int = 0
    seen_ids: set[str] | None = None

    def should_notify(
        self,
        point: TradeActionPoint,
        *,
        max_actions: int,
        max_pairs: int,
    ) -> bool:
        if self.seen_ids is None:
            self.seen_ids = set()
        if point.id in self.seen_ids:
            return False
        self.seen_ids.add(point.id)

        if self.action_count >= max_actions or self.pair_count >= max_pairs:
            return False

        if self.pending_open is None:
            self.pending_open = point
            self.action_count += 1
            return True

        if point.action == self.pending_open.action:
            if _better_open(point, self.pending_open):
                self.pending_open = point
                return True
            return False

        pair = _make_pair(self.pending_open, point)
        if not should_close_tracked_pair(pair):
            return False

        self.pending_open = None
        self.action_count += 1
        self.pair_count += 1
        return True


def _select_symbol_open_reprice_pairs(
    points: Sequence[TradeActionPoint],
    *,
    symbol: str,
    max_actions: int | None,
    max_pairs: int | None,
    require_directional_close: bool,
) -> tuple[list[TTradePair], list[TradeActionPoint]]:
    pending_open: TradeActionPoint | None = None
    pairs: list[TTradePair] = []
    unpaired: list[TradeActionPoint] = []
    action_count = 0

    for point in sorted(points, key=lambda item: (item.timestamp, item.id)):
        if max_actions is not None and action_count >= max_actions:
            unpaired.append(point)
            continue
        if max_pairs is not None and len(pairs) >= max_pairs:
            unpaired.append(point)
            continue

        if pending_open is None:
            pending_open = point
            action_count += 1
            continue

        if point.action == pending_open.action:
            if _better_open(point, pending_open):
                unpaired.append(pending_open)
                pending_open = point
            else:
                unpaired.append(point)
            continue

        pair = _make_pair(pending_open, point)
        if require_directional_close and not should_close_tracked_pair(pair):
            unpaired.append(point)
            continue
        pairs.append(pair)
        pending_open = None
        action_count += 1

    if pending_open is not None:
        unpaired.append(pending_open)
    return pairs, unpaired


def should_close_tracked_pair(pair: TTradePair) -> bool:
    close_time = pair.sell.timestamp.strftime("%H:%M") if pair.order == "buy->sell" else pair.buy.timestamp.strftime("%H:%M")
    if pair.order == "sell->buy":
        return pair.spread_pct >= 1.0 or (close_time >= "14:30" and pair.spread > 0)
    return (
        pair.spread_pct >= 3.0
        or (close_time >= "13:00" and pair.spread_pct >= 1.0)
        or (close_time >= "14:30" and pair.spread > 0)
    )


def _make_pair(left: TradeActionPoint, right: TradeActionPoint) -> TTradePair:
    if left.action == "buy":
        buy, sell = left, right
    else:
        buy, sell = right, left
    spread = round(sell.price - buy.price, 4)
    reference_price = buy.price if buy.timestamp <= sell.timestamp else sell.price
    spread_pct = spread / reference_price * 100 if reference_price else 0
    return TTradePair(
        symbol=buy.symbol,
        buy=buy,
        sell=sell,
        spread=spread,
        spread_pct=round(spread_pct, 4),
    )


def _better_open(candidate: TradeActionPoint, current: TradeActionPoint) -> bool:
    if candidate.action == "buy":
        return candidate.price < current.price
    return candidate.price > current.price
