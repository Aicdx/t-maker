from datetime import datetime

from tmaker.domain.models import Candle
from tmaker.market.bars import aggregate_five_minute, append_candle, filter_trading_minutes


def make_candle(minute: int, close: float, volume: float = 100) -> Candle:
    return Candle(
        symbol="600000",
        timestamp=datetime(2026, 6, 5, 9, minute),
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.3,
        close=close,
        volume=volume,
    )


def test_aggregate_five_minute_builds_ohlcv_from_complete_group() -> None:
    candles = [
        make_candle(31, 10.0, 100),
        make_candle(32, 10.2, 120),
        make_candle(33, 10.1, 90),
        make_candle(34, 10.4, 130),
        make_candle(35, 10.3, 110),
    ]

    bars = aggregate_five_minute(candles)

    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 6, 5, 9, 35)
    assert bars[0].open == 9.9
    assert bars[0].high == 10.6
    assert bars[0].low == 9.7
    assert bars[0].close == 10.3
    assert bars[0].volume == 550


def test_aggregate_five_minute_ignores_incomplete_group() -> None:
    candles = [make_candle(31, 10.0), make_candle(32, 10.2), make_candle(33, 10.1)]

    assert aggregate_five_minute(candles) == []


def test_aggregate_five_minute_does_not_cross_lunch_break() -> None:
    morning = [
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 11, 28), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 11, 29), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 11, 30), open=10, high=10, low=10, close=10, volume=100),
    ]
    afternoon = [
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 13, 1), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 13, 2), open=10, high=10, low=10, close=10, volume=100),
    ]

    assert aggregate_five_minute(morning + afternoon) == []


def test_filter_trading_minutes_drops_lunch_and_after_close_candles() -> None:
    candles = [
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 9, 29), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 9, 30), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 11, 30), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 11, 31), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 13, 0), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 15, 0), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 15, 1), open=10, high=10, low=10, close=10, volume=100),
    ]

    filtered = filter_trading_minutes(candles)

    assert [candle.timestamp.strftime("%H:%M") for candle in filtered] == [
        "09:30",
        "11:30",
        "13:00",
        "15:00",
    ]


def test_aggregate_five_minute_ignores_after_close_candles() -> None:
    candles = [
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 56), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 57), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 58), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 59), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 15, 0), open=10, high=10, low=10, close=11, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 15, 1), open=11, high=11, low=11, close=12, volume=100),
    ]

    bars = aggregate_five_minute(candles)

    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 6, 5, 15, 0)
    assert bars[0].close == 11


def test_aggregate_five_minute_closes_on_natural_market_boundary() -> None:
    candles = [
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 55), open=9, high=9, low=9, close=9, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 56), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 57), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 58), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 14, 59), open=10, high=10, low=10, close=10, volume=100),
        Candle(symbol="600000", timestamp=datetime(2026, 6, 5, 15, 0), open=10, high=10, low=10, close=11, volume=100),
    ]

    bars = aggregate_five_minute(candles)

    assert bars[-1].timestamp == datetime(2026, 6, 5, 15, 0)
    assert bars[-1].open == 10
    assert bars[-1].close == 11


def test_append_candle_replaces_duplicate_timestamp_and_keeps_order() -> None:
    candles: list[Candle] = []
    first = make_candle(31, 10.0)
    duplicate = make_candle(31, 10.3)
    second = make_candle(32, 10.1)

    candles = append_candle(candles, first)
    candles = append_candle(candles, second)
    candles = append_candle(candles, duplicate)

    assert [c.timestamp.minute for c in candles] == [31, 32]
    assert candles[0].close == 10.3
