from datetime import datetime, timedelta

from tmaker.domain.models import Candle
from tmaker.strategy.market_context import build_equal_weight_sector_candles, build_market_context


def test_build_market_context_compares_stock_sector_and_index() -> None:
    stock = _candles("300308", [100, 104, 108])
    sector = _candles("SECTOR_PROXY", [100, 102, 103])
    index = _candles("399006", [100, 101, 101.5])

    context = build_market_context(stock, index_candles=index, sector_candles=sector)

    assert round(context.stock_vs_sector_pct, 2) == 5.0
    assert round(context.sector_relative_strength_pct, 2) == 1.5
    assert context.index_trend == "up"
    assert context.sector_trend == "up"


def test_build_equal_weight_sector_candles_uses_peer_symbols_only() -> None:
    candles = {
        "300308": _candles("300308", [100, 110]),
        "300502": _candles("300502", [200, 204]),
        "600487": _candles("600487", [50, 52]),
    }

    sector = build_equal_weight_sector_candles("300308", candles)

    assert [candle.symbol for candle in sector] == ["SECTOR_PROXY", "SECTOR_PROXY"]
    assert sector[0].close == 125
    assert sector[1].close == 128
    assert sector[1].volume == 2000


def _candles(symbol: str, closes: list[float]) -> list[Candle]:
    start = datetime(2026, 6, 5, 9, 30)
    return [
        Candle(
            symbol=symbol,
            timestamp=start + timedelta(minutes=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000,
        )
        for index, close in enumerate(closes)
    ]
