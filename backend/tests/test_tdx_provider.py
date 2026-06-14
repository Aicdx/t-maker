from datetime import date, datetime
from types import SimpleNamespace

from tmaker.market.tdx_provider import (
    TdxHistoricalMinuteProvider,
    _minute_rows_to_candles,
    _security_bar_to_candle,
)


def test_security_bar_to_candle_normalizes_tdx_lot_volume() -> None:
    row = SimpleNamespace(
        year=2026,
        month=5,
        day=28,
        hour=10,
        minute=1,
        open=150.0,
        high=151.0,
        low=149.5,
        close=150.8,
        vol=120000.0,
    )

    candle = _security_bar_to_candle("300308", row)

    assert candle.symbol == "300308"
    assert candle.timestamp == datetime(2026, 5, 28, 10, 1)
    assert candle.open == 150.0
    assert candle.high == 151.0
    assert candle.low == 149.5
    assert candle.close == 150.8
    assert candle.volume == 1200.0


def test_history_minute_rows_start_from_0931_and_use_previous_price_as_open() -> None:
    rows = [
        SimpleNamespace(price=150.2, vol=1200),
        SimpleNamespace(price=150.8, vol=900),
    ]

    candles = _minute_rows_to_candles("300308", date(2026, 5, 28), rows)

    assert [candle.timestamp for candle in candles] == [
        datetime(2026, 5, 28, 9, 31),
        datetime(2026, 5, 28, 9, 32),
    ]
    assert candles[0].open == 150.2
    assert candles[0].close == 150.2
    assert candles[1].open == 150.2
    assert candles[1].close == 150.8


def test_fetch_minutes_for_date_prefers_true_ohlc_security_bars() -> None:
    class FakeTdxProvider(TdxHistoricalMinuteProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def _fetch_paged_security_bars(self, symbol: str, trade_date: date) -> list:
            self.calls.append("security_bars")
            return [
                _security_bar_to_candle(
                    symbol,
                    SimpleNamespace(
                        year=trade_date.year,
                        month=trade_date.month,
                        day=trade_date.day,
                        hour=9,
                        minute=31,
                        open=150.0,
                        high=151.2,
                        low=149.8,
                        close=150.6,
                        vol=180000.0,
                    ),
                )
            ]

        def _fetch_history_minutes(self, symbol: str, trade_date: date) -> list:
            self.calls.append("history_minutes")
            return _minute_rows_to_candles(
                symbol,
                trade_date,
                [SimpleNamespace(price=150.6, vol=1800)],
            )

    provider = FakeTdxProvider()

    candles = provider.fetch_minutes_for_date("300308", date(2026, 5, 28))

    assert provider.calls == ["security_bars"]
    assert candles[0].open == 150.0
    assert candles[0].high == 151.2
    assert candles[0].low == 149.8
