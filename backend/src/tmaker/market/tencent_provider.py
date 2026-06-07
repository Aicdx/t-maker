from __future__ import annotations

from datetime import datetime

import httpx

from tmaker.domain.models import Candle, MarketQuote
from tmaker.market.akshare_provider import MarketDataUnavailable
from tmaker.market.bars import filter_trading_minutes


class TencentMinuteProvider:
    minute_url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=10,
            trust_env=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://gu.qq.com/",
            },
        )

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        market_symbol = _to_market_symbol(symbol)
        response = self.client.get(self.minute_url, params={"code": market_symbol})
        response.raise_for_status()
        payload = response.json()
        node = payload.get("data", {}).get(market_symbol, {}).get("data", {})
        date_text = node.get("date")
        rows = node.get("data") or []
        if not date_text or not rows:
            raise MarketDataUnavailable(f"No Tencent minute data returned for {symbol}")
        return filter_trading_minutes(_rows_to_candles(_plain_symbol(market_symbol), date_text, rows))


class TencentMarketProvider(TencentMinuteProvider):
    quote_url = "https://qt.gtimg.cn/q="

    def fetch_quote(self, symbol: str) -> MarketQuote:
        market_symbol = _to_market_symbol(symbol)
        response = self.client.get(self.quote_url, params={"q": market_symbol})
        response.raise_for_status()
        return _quote_text_to_market_quote(response.content.decode("gbk", errors="ignore"), market_symbol)


class TencentHistoricalMinuteProvider(TencentMinuteProvider):
    historical_url = "https://web.ifzq.gtimg.cn/appstock/app/day/query"

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        market_symbol = _to_market_symbol(symbol)
        response = self.client.get(self.historical_url, params={"code": market_symbol})
        response.raise_for_status()
        payload = response.json()
        days = payload.get("data", {}).get(market_symbol, {}).get("data", [])
        candles: list[Candle] = []
        for day in days:
            date_text = day.get("date")
            rows = day.get("data") or []
            if date_text and rows:
                candles.extend(_rows_to_candles(_plain_symbol(market_symbol), date_text, rows))
        if not candles:
            raise MarketDataUnavailable(f"No Tencent historical minute data returned for {symbol}")
        return sorted(filter_trading_minutes(candles), key=lambda candle: candle.timestamp)


def _rows_to_candles(symbol: str, date_text: str, rows: list[str]) -> list[Candle]:
    trade_date = datetime.strptime(date_text, "%Y%m%d").date()
    candles: list[Candle] = []
    previous_price: float | None = None
    previous_cumulative_volume = 0.0

    for row in rows:
        parts = row.split()
        if len(parts) < 3:
            continue
        minute_text, price_text, cumulative_volume_text = parts[:3]
        timestamp = datetime.combine(
            trade_date,
            datetime.strptime(minute_text, "%H%M").time(),
        )
        close = float(price_text)
        cumulative_volume = float(cumulative_volume_text)
        minute_volume = max(0.0, cumulative_volume - previous_cumulative_volume)
        open_price = previous_price if previous_price is not None else close
        candles.append(
            Candle(
                symbol=symbol,
                timestamp=timestamp,
                open=open_price,
                high=max(open_price, close),
                low=min(open_price, close),
                close=close,
                volume=minute_volume,
            )
        )
        previous_price = close
        previous_cumulative_volume = cumulative_volume

    if not candles:
        raise MarketDataUnavailable(f"No valid Tencent minute rows returned for {symbol}")
    return candles


def _quote_text_to_market_quote(text: str, market_symbol: str) -> MarketQuote:
    _, _, payload = text.partition('"')
    payload, _, _ = payload.partition('"')
    fields = payload.split("~")
    if len(fields) < 35:
        raise MarketDataUnavailable(f"No Tencent quote data returned for {_plain_symbol(market_symbol)}")
    return MarketQuote(
        symbol=fields[2] or _plain_symbol(market_symbol),
        name=fields[1],
        latest=_to_float(fields[3]),
        previous_close=_to_float(fields[4]),
        open=_to_float(fields[5]),
        high=_to_float(fields[33]),
        low=_to_float(fields[34]),
        change=_to_float(fields[31]),
        change_percent=_to_float(fields[32]),
    )


def _to_float(value: str) -> float:
    return float(value) if value else 0.0


def _to_market_symbol(symbol: str) -> str:
    normalized = symbol.lower()
    if normalized.startswith(("sh", "sz", "bj")) and len(normalized) == 8:
        return normalized
    if normalized.startswith(("6", "5")):
        return f"sh{normalized}"
    if normalized.startswith(("0", "1", "2", "3")):
        return f"sz{normalized}"
    if normalized.startswith(("4", "8", "9")):
        return f"bj{normalized}"
    return normalized


def _plain_symbol(symbol: str) -> str:
    if symbol.startswith(("sh", "sz", "bj")):
        return symbol[2:]
    return symbol
