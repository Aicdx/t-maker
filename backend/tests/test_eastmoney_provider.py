from __future__ import annotations

from datetime import datetime, date

import httpx

from tmaker.market.eastmoney_provider import EastmoneyHistoricalMinuteProvider


def test_eastmoney_provider_fetches_requested_trade_date_minute_bars() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/qt/stock/kline/get"
        assert request.headers.get("host") == "push2his.eastmoney.com"
        assert request.url.params["secid"] == "0.300308"
        assert request.url.params["klt"] == "1"
        assert request.url.params["beg"] == "20260528"
        assert request.url.params["end"] == "20260528"
        return httpx.Response(
            200,
            json={
                "rc": 0,
                "data": {
                    "code": "300308",
                    "klines": [
                        "2026-05-28 09:30,150.00,150.20,150.50,149.80,1200,0,0",
                        "2026-05-28 09:31,150.20,150.10,150.30,149.90,900,0,0",
                    ],
                },
            },
        )

    provider = EastmoneyHistoricalMinuteProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))

    candles = provider.fetch_minutes_for_date("300308", date(2026, 5, 28))

    assert [candle.timestamp for candle in candles] == [
        datetime(2026, 5, 28, 9, 30),
        datetime(2026, 5, 28, 9, 31),
    ]
    assert candles[0].symbol == "300308"
    assert candles[0].open == 150.0
    assert candles[0].close == 150.2
    assert candles[0].high == 150.5
    assert candles[0].low == 149.8
    assert candles[0].volume == 1200


def test_eastmoney_provider_falls_back_to_resolved_ipv4_host_header() -> None:
    seen_urls: list[str] = []
    seen_hosts: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        seen_hosts.append(request.headers.get("host"))
        if request.url.host == "push2his.eastmoney.com":
            raise httpx.RemoteProtocolError("disconnected")
        return httpx.Response(
            200,
            json={
                "rc": 0,
                "data": {
                    "code": "300308",
                    "klines": ["2026-05-28 09:30,150.00,150.20,150.50,149.80,1200,0,0"],
                },
            },
        )

    provider = EastmoneyHistoricalMinuteProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))

    candles = provider.fetch_minutes_for_date("300308", date(2026, 5, 28))

    assert len(candles) == 1
    assert "103.220.167.80" in seen_urls[0]
    assert seen_hosts[0] == "push2his.eastmoney.com"


def test_eastmoney_provider_uses_exchange_specific_index_secids() -> None:
    seen_secids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_secids.append(str(request.url.params["secid"]))
        return httpx.Response(
            200,
            json={
                "rc": 0,
                "data": {
                    "klines": ["2026-06-03 09:30,100.00,100.20,100.50,99.80,1200,0,0"],
                },
            },
        )

    provider = EastmoneyHistoricalMinuteProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))

    provider.fetch_minutes_for_date("399006", date(2026, 6, 3))
    provider.fetch_minutes_for_date("000300", date(2026, 6, 3))
    provider.fetch_minutes_for_date("000001", date(2026, 6, 3))

    assert seen_secids == ["0.399006", "1.000300", "1.000001"]
