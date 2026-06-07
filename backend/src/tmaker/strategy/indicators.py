from __future__ import annotations

from pydantic import BaseModel

from tmaker.domain.models import Candle


class IndicatorSnapshot(BaseModel):
    vwap: float = 0
    price_vwap_deviation_pct: float = 0
    session_vwap: float = 0
    price_session_vwap_deviation_pct: float = 0
    intraday_change_pct: float = 0
    volume_ratio: float = 0
    relative_strength_pct: float = 0


def compute_indicators(
    candles: list[Candle],
    index_candles: list[Candle] | None = None,
    session_candles: list[Candle] | None = None,
) -> IndicatorSnapshot:
    if not candles:
        return IndicatorSnapshot()

    latest = candles[-1]
    vwap = _vwap(candles)
    deviation = ((latest.close - vwap) / vwap * 100) if vwap else 0
    session = session_candles or candles
    session_vwap = _vwap(session)
    session_deviation = ((latest.close - session_vwap) / session_vwap * 100) if session_vwap else 0
    intraday_change = ((latest.close - session[0].open) / session[0].open * 100) if session[0].open else 0
    volume_ratio = _volume_ratio(candles)
    relative_strength = _relative_strength(candles, index_candles or [])

    return IndicatorSnapshot(
        vwap=vwap,
        price_vwap_deviation_pct=deviation,
        session_vwap=session_vwap,
        price_session_vwap_deviation_pct=session_deviation,
        intraday_change_pct=intraday_change,
        volume_ratio=volume_ratio,
        relative_strength_pct=relative_strength,
    )


def _vwap(candles: list[Candle]) -> float:
    total_volume = sum(candle.volume for candle in candles)
    if total_volume == 0:
        return 0
    return sum(candle.close * candle.volume for candle in candles) / total_volume


def _volume_ratio(candles: list[Candle]) -> float:
    if not candles:
        return 0
    average_volume = sum(candle.volume for candle in candles) / len(candles)
    if average_volume == 0:
        return 0
    return candles[-1].volume / average_volume


def _relative_strength(stock: list[Candle], index: list[Candle]) -> float:
    if len(stock) < 2 or len(index) < 2:
        return 0
    stock_return = (stock[-1].close - stock[0].close) / stock[0].close * 100
    index_return = (index[-1].close - index[0].close) / index[0].close * 100
    return stock_return - index_return
