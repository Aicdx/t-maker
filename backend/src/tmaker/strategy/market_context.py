from __future__ import annotations

from collections.abc import Sequence

from tmaker.domain.models import Candle
from tmaker.strategy.rules import MarketContext


def build_market_context(
    stock_candles: Sequence[Candle],
    *,
    index_candles: Sequence[Candle] | None = None,
    sector_candles: Sequence[Candle] | None = None,
) -> MarketContext:
    stock_change = _change_pct(stock_candles)
    index_change = _change_pct(index_candles or [])
    sector_change = _change_pct(sector_candles or [])
    return MarketContext(
        index_change_pct=index_change,
        sector_change_pct=sector_change,
        sector_relative_strength_pct=sector_change - index_change,
        stock_vs_sector_pct=stock_change - sector_change,
        sector_trend=_trend(sector_candles or []),
        index_trend=_trend(index_candles or []),
    )


def build_equal_weight_sector_candles(symbol: str, candles_by_symbol: dict[str, list[Candle]]) -> list[Candle]:
    peer_series = {
        peer_symbol: candles
        for peer_symbol, candles in candles_by_symbol.items()
        if peer_symbol != symbol and candles
    }
    if not peer_series:
        return []

    timestamps = sorted(
        set.intersection(*(set(candle.timestamp for candle in candles) for candles in peer_series.values()))
    )
    sector_candles: list[Candle] = []
    for timestamp in timestamps:
        peers = [
            next(candle for candle in candles if candle.timestamp == timestamp)
            for candles in peer_series.values()
        ]
        sector_candles.append(
            Candle(
                symbol="SECTOR_PROXY",
                timestamp=timestamp,
                open=sum(candle.open for candle in peers) / len(peers),
                high=sum(candle.high for candle in peers) / len(peers),
                low=sum(candle.low for candle in peers) / len(peers),
                close=sum(candle.close for candle in peers) / len(peers),
                volume=sum(candle.volume for candle in peers),
            )
        )
    return sector_candles


def _change_pct(candles: Sequence[Candle]) -> float:
    if len(candles) < 2:
        return 0
    first = candles[0]
    latest = candles[-1]
    return ((latest.close - first.open) / first.open * 100) if first.open else 0


def _trend(candles: Sequence[Candle]) -> str:
    if len(candles) < 3:
        return "unknown"
    recent = candles[-3:]
    if recent[-1].close > recent[0].close:
        return "up"
    if recent[-1].close < recent[0].close:
        return "down"
    return "flat"
