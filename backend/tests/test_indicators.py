from datetime import datetime, timedelta

from tmaker.domain.models import Candle
from tmaker.strategy.indicators import IndicatorSnapshot, compute_indicators


def make_series(symbol: str, closes: list[float], volumes: list[float]) -> list[Candle]:
    start = datetime(2026, 6, 5, 9, 31)
    return [
        Candle(
            symbol=symbol,
            timestamp=start + timedelta(minutes=index),
            open=close - 0.1,
            high=close + 0.2,
            low=close - 0.2,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def test_compute_indicators_returns_vwap_deviation_and_volume_ratio() -> None:
    candles = make_series("600000", [10.0, 10.2, 10.4], [100, 200, 300])

    snapshot = compute_indicators(candles)

    assert isinstance(snapshot, IndicatorSnapshot)
    assert round(snapshot.vwap, 4) == 10.2667
    assert round(snapshot.price_vwap_deviation_pct, 4) == 1.2987
    assert snapshot.volume_ratio == 1.5


def test_compute_indicators_returns_relative_strength_against_index() -> None:
    stock = make_series("600000", [10.0, 10.6], [100, 100])
    index = make_series("000001", [3000.0, 3030.0], [1000, 1000])

    snapshot = compute_indicators(stock, index)

    assert round(snapshot.relative_strength_pct, 4) == 5.0


def test_compute_indicators_handles_empty_input() -> None:
    snapshot = compute_indicators([])

    assert snapshot.vwap == 0
    assert snapshot.price_vwap_deviation_pct == 0
    assert snapshot.volume_ratio == 0
    assert snapshot.relative_strength_pct == 0
