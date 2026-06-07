from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from tmaker.market.tencent_provider import (
    TencentHistoricalMinuteProvider,
    TencentMarketProvider,
    TencentMinuteProvider,
)


def test_tencent_provider_converts_minute_payload_to_candles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["code"] == "sh600000"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "sh600000": {
                        "data": {
                            "date": "20260605",
                            "data": [
                                "0930 9.18 1372 1259496.00",
                                "0931 9.21 10724 9857156.00",
                                "0932 9.22 20688 19036490.00",
                            ],
                        }
                    }
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TencentMinuteProvider(client=client)

    candles = provider.fetch_minutes("600000")

    assert candles[0].symbol == "600000"
    assert candles[0].timestamp == datetime(2026, 6, 5, 9, 30)
    assert candles[0].open == 9.18
    assert candles[0].high == 9.18
    assert candles[0].low == 9.18
    assert candles[0].close == 9.18
    assert candles[0].volume == 1372
    assert candles[1].open == 9.18
    assert candles[1].high == 9.21
    assert candles[1].low == 9.18
    assert candles[1].close == 9.21
    assert candles[1].volume == 9352


def test_tencent_provider_converts_quote_payload_to_market_quote() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "sz300308"
        return httpx.Response(
            200,
            content=(
                'v_sz300308="51~中际旭创~300308~1179.99~1280.00~1273.20~474741~223215~'
                '251526~1179.99~8~1179.98~2~1179.97~2~1179.95~50~1179.91~1~1180.00~659~'
                '1180.01~13~1180.02~2~1180.05~1~1180.25~2~~20260605161445~-100.01~'
                '-7.81~1301.51~1160.00~1179.99/474741/58324832702";'
            ).encode("gbk"),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TencentMarketProvider(client=client)

    quote = provider.fetch_quote("300308")

    assert quote.symbol == "300308"
    assert quote.name == "中际旭创"
    assert quote.latest == 1179.99
    assert quote.previous_close == 1280.0
    assert quote.open == 1273.2
    assert quote.high == 1301.51
    assert quote.low == 1160.0
    assert quote.change == -100.01
    assert quote.change_percent == -7.81


def test_tencent_historical_provider_converts_multiple_days_to_candles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["code"] == "sh600487"
        return httpx.Response(
            200,
            json={
                "data": {
                    "sh600487": {
                        "data": [
                            {
                                "date": "20260605",
                                "data": [
                                    "0930 96.00 100 960000.00",
                                    "0931 96.50 180 1737000.00",
                                ],
                            },
                            {
                                "date": "20260604",
                                "data": [
                                    "0930 95.00 90 855000.00",
                                    "0931 94.80 150 1422000.00",
                                ],
                            },
                        ]
                    }
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TencentHistoricalMinuteProvider(client=client)

    candles = provider.fetch_minutes("600487")

    assert [candle.timestamp.strftime("%Y-%m-%d %H:%M") for candle in candles] == [
        "2026-06-04 09:30",
        "2026-06-04 09:31",
        "2026-06-05 09:30",
        "2026-06-05 09:31",
    ]
    assert candles[0].symbol == "600487"
    assert candles[-1].volume == 80


def test_tencent_provider_filters_non_trading_minutes() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "sh600000": {
                        "data": {
                            "date": "20260605",
                            "data": [
                                "0929 9.10 100 91000.00",
                                "0930 9.18 200 183600.00",
                                "1131 9.20 300 276000.00",
                                "1500 9.34 400 373600.00",
                                "1501 9.35 500 467500.00",
                            ],
                        }
                    }
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TencentMinuteProvider(client=client)

    candles = provider.fetch_minutes("600000")

    assert [candle.timestamp.strftime("%H:%M") for candle in candles] == ["09:30", "15:00"]


def test_tencent_provider_accepts_prefixed_symbol() -> None:
    seen_codes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_codes.append(str(request.url.params["code"]))
        return httpx.Response(
            200,
            json={
                "data": {
                    "sz000001": {
                        "data": {
                            "date": "20260605",
                            "data": ["0930 10.00 100 100000.00"],
                        }
                    }
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TencentMinuteProvider(client=client)

    candles = provider.fetch_minutes("sz000001")

    assert seen_codes == ["sz000001"]
    assert candles[0].symbol == "000001"


def test_tencent_provider_raises_when_payload_has_no_data() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    provider = TencentMinuteProvider(client=client)

    with pytest.raises(RuntimeError, match="No Tencent minute data"):
        provider.fetch_minutes("600000")
