from datetime import date, datetime

import pandas as pd
import pytest

from tmaker.market.akshare_provider import AkshareMinuteProvider, MarketDataUnavailable


class FakeAkshare:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[dict[str, str]] = []

    def stock_zh_a_hist_min_em(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        period: str,
        adjust: str,
    ) -> pd.DataFrame:
        self.calls.append(
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "period": period,
                "adjust": adjust,
            }
        )
        return self.frame


def test_akshare_provider_converts_chinese_minute_columns_to_candles() -> None:
    frame = pd.DataFrame(
        [
            {
                "时间": "2026-06-05 09:31:00",
                "开盘": 10.0,
                "收盘": 10.2,
                "最高": 10.3,
                "最低": 9.9,
                "成交量": 1200,
            },
            {
                "时间": "2026-06-05 09:32:00",
                "开盘": 10.2,
                "收盘": 10.1,
                "最高": 10.25,
                "最低": 10.0,
                "成交量": 900,
            },
        ]
    )
    fake = FakeAkshare(frame)
    provider = AkshareMinuteProvider(akshare_module=fake)

    candles = provider.fetch_minutes("600000")

    assert fake.calls[0]["symbol"] == "600000"
    assert fake.calls[0]["period"] == "1"
    assert candles[0].timestamp == datetime(2026, 6, 5, 9, 31)
    assert candles[0].open == 10.0
    assert candles[0].high == 10.3
    assert candles[0].low == 9.9
    assert candles[0].close == 10.2
    assert candles[0].volume == 1200
    assert candles[1].symbol == "600000"


def test_akshare_provider_fetches_one_requested_trade_date() -> None:
    frame = pd.DataFrame(
        [
            {
                "时间": "2026-05-28 09:31:00",
                "开盘": 10.0,
                "收盘": 10.2,
                "最高": 10.3,
                "最低": 9.9,
                "成交量": 1200,
            }
        ]
    )
    fake = FakeAkshare(frame)
    provider = AkshareMinuteProvider(akshare_module=fake)

    candles = provider.fetch_minutes_for_date("600000", date(2026, 5, 28))

    assert fake.calls[0]["start_date"] == "2026-05-28 09:30:00"
    assert fake.calls[0]["end_date"] == "2026-05-28 15:00:00"
    assert [candle.timestamp.date() for candle in candles] == [date(2026, 5, 28)]


def test_akshare_provider_raises_when_frame_is_empty() -> None:
    provider = AkshareMinuteProvider(akshare_module=FakeAkshare(pd.DataFrame()))

    with pytest.raises(MarketDataUnavailable):
        provider.fetch_minutes("600000")


def test_akshare_provider_temporarily_bypasses_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    frame = pd.DataFrame(
        [
            {
                "时间": "2026-06-05 09:31:00",
                "开盘": 10.0,
                "收盘": 10.2,
                "最高": 10.3,
                "最低": 9.9,
                "成交量": 1200,
            }
        ]
    )

    class ProxyInspectingAkshare(FakeAkshare):
        def stock_zh_a_hist_min_em(
            self,
            symbol: str,
            start_date: str,
            end_date: str,
            period: str,
            adjust: str,
        ) -> pd.DataFrame:
            import os

            assert "HTTP_PROXY" not in os.environ
            assert "HTTPS_PROXY" not in os.environ
            assert os.environ["NO_PROXY"] == "*"
            assert os.environ["no_proxy"] == "*"
            return super().stock_zh_a_hist_min_em(symbol, start_date, end_date, period, adjust)

    provider = AkshareMinuteProvider(akshare_module=ProxyInspectingAkshare(frame))

    provider.fetch_minutes("600000")

    import os

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert "NO_PROXY" not in os.environ
    assert "no_proxy" not in os.environ
