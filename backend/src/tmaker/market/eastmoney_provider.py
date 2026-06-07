from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Any

import httpx
import requests

from tmaker.domain.models import Candle
from tmaker.market.akshare_provider import MarketDataChannelUnavailable, MarketDataUnavailable
from tmaker.market.bars import filter_trading_minutes


class EastmoneyHistoricalMinuteProvider:
    kline_url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    kline_fallback_url = "http://103.220.167.80/api/qt/stock/kline/get"

    def __init__(self, client: Any | None = None) -> None:
        self.client = client or _requests_session()

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        raise MarketDataUnavailable("Eastmoney provider requires a requested trade date")

    def fetch_minutes_for_date(self, symbol: str, trade_date: date_type) -> list[Candle]:
        params = {
            "secid": _to_secid(symbol),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "1",
            "fqt": "1",
            "beg": trade_date.strftime("%Y%m%d"),
            "end": trade_date.strftime("%Y%m%d"),
        }
        response = self._get_with_fallback(params)
        payload = response.json()
        rows = payload.get("data", {}).get("klines") or []
        candles = [_row_to_candle(symbol, row) for row in rows]
        candles = [candle for candle in candles if candle.timestamp.date() == trade_date]
        if not candles:
            raise MarketDataUnavailable(f"No Eastmoney minute data returned for {symbol} on {trade_date.isoformat()}")
        return sorted(filter_trading_minutes(candles), key=lambda candle: candle.timestamp)

    def _get_with_fallback(self, params: dict[str, str]) -> Any:
        errors: list[Exception] = []
        for url, headers in [
            (self.kline_fallback_url, {"Host": "push2his.eastmoney.com"}),
            (self.kline_url, {}),
        ]:
            try:
                response = (
                    self.client.get(url, params=params, headers=headers, timeout=20)
                    if isinstance(self.client, requests.Session)
                    else self.client.get(url, params=params, headers=headers)
                )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, requests.RequestException) as exc:
                errors.append(exc)
        raise MarketDataChannelUnavailable(f"Eastmoney minute data channel unavailable: {errors[-1]}")


def _to_secid(symbol: str) -> str:
    normalized = symbol.lower()
    if normalized.startswith("sh"):
        return f"1.{normalized[2:]}"
    if normalized.startswith("sz"):
        return f"0.{normalized[2:]}"
    if normalized.startswith("bj"):
        return f"0.{normalized[2:]}"
    if normalized == "399006":
        return f"0.{normalized}"
    if normalized in {"000001", "000300"}:
        return f"1.{normalized}"
    if normalized.startswith(("6", "5")):
        return f"1.{normalized}"
    return f"0.{normalized}"


def _requests_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://quote.eastmoney.com/",
        }
    )
    return session


def _row_to_candle(symbol: str, row: str) -> Candle:
    parts = row.split(",")
    if len(parts) < 6:
        raise MarketDataUnavailable(f"Invalid Eastmoney minute row for {symbol}: {row}")
    return Candle(
        symbol=symbol[-6:] if symbol.lower().startswith(("sh", "sz", "bj")) else symbol,
        timestamp=datetime.strptime(parts[0], "%Y-%m-%d %H:%M"),
        open=float(parts[1]),
        close=float(parts[2]),
        high=float(parts[3]),
        low=float(parts[4]),
        volume=float(parts[5]),
    )
