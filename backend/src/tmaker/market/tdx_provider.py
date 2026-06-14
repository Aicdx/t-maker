from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Any

from tmaker.domain.models import Candle
from tmaker.market.akshare_provider import MarketDataUnavailable
from tmaker.market.bars import filter_trading_minutes


class TdxHistoricalMinuteProvider:
    def __init__(self, host: str = "218.75.126.9", port: int = 7709, timeout: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        return self._fetch_security_bars(symbol, start=0, count=800)

    def fetch_minutes_for_date(self, symbol: str, trade_date: date_type) -> list[Candle]:
        candles = self._fetch_paged_security_bars(symbol, trade_date)
        if candles:
            return candles

        candles = self._fetch_history_minutes(symbol, trade_date)
        if candles:
            return candles

        raise MarketDataUnavailable(f"No TDX minute data returned for {symbol} on {trade_date.isoformat()}")

    def _fetch_history_minutes(self, symbol: str, trade_date: date_type) -> list[Candle]:
        try:
            from xmtdx import Market, TdxClient
        except ImportError as exc:
            raise MarketDataUnavailable("xmtdx is not installed") from exc

        client = _connect_tdx_client(TdxClient, self.host, self.port, self.timeout)
        try:
            rows = client.get_history_minute_time_data(
                _market_for_symbol(symbol, Market),
                _normalize_symbol(symbol),
                _tdx_date(trade_date),
            )
            candles = _minute_rows_to_candles(_normalize_symbol(symbol), trade_date, rows)
        except Exception as exc:
            raise MarketDataUnavailable(
                f"TDX history minute data unavailable for {symbol} on {trade_date.isoformat()}: {exc}"
            ) from exc
        finally:
            _close_tdx_client(client)
        return sorted(filter_trading_minutes(candles), key=lambda candle: candle.timestamp)

    def _fetch_paged_security_bars(self, symbol: str, trade_date: date_type) -> list[Candle]:
        candles: list[Candle] = []
        for start in range(0, 8000, 800):
            page = self._fetch_security_bars(symbol, start=start, count=800)
            candles.extend(candle for candle in page if candle.timestamp.date() == trade_date)
            if page and page[0].timestamp.date() < trade_date:
                break
        return sorted(filter_trading_minutes(candles), key=lambda candle: candle.timestamp)

    def _fetch_security_bars(self, symbol: str, start: int, count: int) -> list[Candle]:
        try:
            from xmtdx import KlineCategory, Market, TdxClient
        except ImportError as exc:
            raise MarketDataUnavailable("xmtdx is not installed") from exc

        normalized = _normalize_symbol(symbol)
        client = _connect_tdx_client(TdxClient, self.host, self.port, self.timeout)
        try:
            rows = client.get_security_bars(
                _market_for_symbol(normalized, Market),
                normalized,
                KlineCategory.MIN_1,
                start,
                count,
            )
            candles = [_security_bar_to_candle(normalized, row) for row in rows]
        except Exception as exc:
            raise MarketDataUnavailable(f"TDX security bars unavailable for {symbol}: {exc}") from exc
        finally:
            _close_tdx_client(client)
        return sorted(filter_trading_minutes(candles), key=lambda candle: candle.timestamp)


def _connect_tdx_client(client_class: Any, host: str, port: int, timeout: float) -> Any:
    client = client_class(host=host, port=port, timeout=timeout)
    try:
        client.connect()
    except Exception as exc:
        raise MarketDataUnavailable(f"TDX connection unavailable at {host}:{port}: {exc}") from exc
    return client


def _close_tdx_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.lower()
    if normalized.startswith(("sh", "sz", "bj")):
        return normalized[2:]
    return normalized


def _market_for_symbol(symbol: str, market_enum: Any) -> Any:
    normalized = _normalize_symbol(symbol)
    if normalized.startswith(("6", "5")) or normalized in {"000001", "000300"}:
        return market_enum.SH
    if normalized.startswith(("4", "8", "9")):
        return market_enum.BJ
    return market_enum.SZ


def _tdx_date(value: date_type) -> int:
    return int(value.strftime("%Y%m%d"))


def _minute_rows_to_candles(symbol: str, trade_date: date_type, rows: list) -> list[Candle]:
    timestamps = _trading_minute_timestamps(trade_date)
    normalized = _normalize_symbol(symbol)
    candles: list[Candle] = []
    previous_price: float | None = None
    for timestamp, row in zip(timestamps, rows, strict=False):
        price = float(row.price)
        open_price = previous_price if previous_price is not None else price
        candles.append(
            Candle(
                symbol=normalized,
                timestamp=timestamp,
                open=open_price,
                high=max(open_price, price),
                low=min(open_price, price),
                close=price,
                volume=float(row.vol),
            )
        )
        previous_price = price
    return candles


def _security_bar_to_candle(symbol: str, row: Any) -> Candle:
    return Candle(
        symbol=_normalize_symbol(symbol),
        timestamp=datetime(row.year, row.month, row.day, row.hour, row.minute),
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=_normalize_tdx_volume(float(row.vol)),
    )


def _normalize_tdx_volume(value: float) -> float:
    return value / 100 if value >= 100 else value


def _trading_minute_timestamps(trade_date: date_type) -> list[datetime]:
    return [
        *[
            datetime(trade_date.year, trade_date.month, trade_date.day, hour, minute)
            for hour, minute in _minute_range(9, 31, 11, 30)
        ],
        *[
            datetime(trade_date.year, trade_date.month, trade_date.day, hour, minute)
            for hour, minute in _minute_range(13, 1, 15, 0)
        ],
    ]


def _minute_range(start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> list[tuple[int, int]]:
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    return [(minute // 60, minute % 60) for minute in range(start, end + 1)]
