from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date as date_type
from datetime import datetime, timedelta
from collections.abc import Iterator
from typing import Any

import akshare as ak
import pandas as pd

from tmaker.domain.models import Candle


class MarketDataUnavailable(RuntimeError):
    """Raised when the market data provider cannot return usable minute bars."""


class MarketDataChannelUnavailable(MarketDataUnavailable):
    """Raised when the market data provider channel cannot be reached."""


class AkshareMinuteProvider:
    def __init__(self, akshare_module: Any = ak, lookback_days: int = 5) -> None:
        self.akshare = akshare_module
        self.lookback_days = lookback_days

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        end = datetime.now()
        start = end - timedelta(days=self.lookback_days)
        with _without_proxy_environment():
            frame = self.akshare.stock_zh_a_hist_min_em(
                symbol=symbol,
                start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                period="1",
                adjust="",
            )
        if frame is None or frame.empty:
            raise MarketDataUnavailable(f"No minute data returned for {symbol}")
        return _frame_to_candles(symbol, frame)

    def fetch_minutes_for_date(self, symbol: str, trade_date: date_type) -> list[Candle]:
        start = datetime.combine(trade_date, datetime.strptime("09:30:00", "%H:%M:%S").time())
        end = datetime.combine(trade_date, datetime.strptime("15:00:00", "%H:%M:%S").time())
        with _without_proxy_environment():
            frame = self.akshare.stock_zh_a_hist_min_em(
                symbol=symbol,
                start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                period="1",
                adjust="",
            )
        if frame is None or frame.empty:
            raise MarketDataUnavailable(f"No minute data returned for {symbol} on {trade_date.isoformat()}")
        candles = [
            candle for candle in _frame_to_candles(symbol, frame) if candle.timestamp.date() == trade_date
        ]
        if not candles:
            raise MarketDataUnavailable(f"No minute data returned for {symbol} on {trade_date.isoformat()}")
        return candles


def _frame_to_candles(symbol: str, frame: pd.DataFrame) -> list[Candle]:
    columns = _resolve_columns(frame)
    candles = [
        Candle(
            symbol=symbol,
            timestamp=pd.to_datetime(row[columns["timestamp"]]).to_pydatetime(),
            open=float(row[columns["open"]]),
            high=float(row[columns["high"]]),
            low=float(row[columns["low"]]),
            close=float(row[columns["close"]]),
            volume=float(row[columns["volume"]]),
        )
        for _, row in frame.iterrows()
    ]
    candles.sort(key=lambda candle: candle.timestamp)
    return candles


def _resolve_columns(frame: pd.DataFrame) -> dict[str, str]:
    candidates = {
        "timestamp": ["时间", "日期", "datetime", "time"],
        "open": ["开盘", "open"],
        "high": ["最高", "high"],
        "low": ["最低", "low"],
        "close": ["收盘", "close"],
        "volume": ["成交量", "volume"],
    }
    resolved: dict[str, str] = {}
    for key, names in candidates.items():
        for name in names:
            if name in frame.columns:
                resolved[key] = name
                break
        else:
            raise MarketDataUnavailable(f"Missing required column for {key}: {list(frame.columns)}")
    return resolved


@contextmanager
def _without_proxy_environment() -> Iterator[None]:
    proxy_keys = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ]
    original = {key: os.environ.get(key) for key in proxy_keys}
    try:
        for key in proxy_keys:
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
