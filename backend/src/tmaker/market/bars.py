from __future__ import annotations

from datetime import datetime

from tmaker.domain.models import Candle


def append_candle(candles: list[Candle], candle: Candle) -> list[Candle]:
    by_timestamp = {existing.timestamp: existing for existing in candles}
    by_timestamp[candle.timestamp] = candle
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def filter_trading_minutes(candles: list[Candle]) -> list[Candle]:
    return [candle for candle in candles if _session_name(candle.timestamp) is not None]


def aggregate_five_minute(candles: list[Candle]) -> list[Candle]:
    ordered = sorted(filter_trading_minutes(candles), key=lambda candle: candle.timestamp)
    groups: list[list[Candle]] = []
    current: list[Candle] = []

    for candle in ordered:
        if not current and _is_session_open(candle.timestamp):
            continue

        if current and not _is_next_minute_same_session(current[-1].timestamp, candle.timestamp):
            current = []

        current.append(candle)
        if _is_five_minute_close(candle.timestamp):
            if len(current) == 5:
                groups.append(current)
            current = []

    return [_build_group(group) for group in groups]


def _is_next_minute_same_session(previous: datetime, current: datetime) -> bool:
    if previous.date() != current.date():
        return False
    if _session_name(previous) != _session_name(current):
        return False
    return (current - previous).total_seconds() == 60


def _session_name(timestamp: datetime) -> str | None:
    minutes = timestamp.hour * 60 + timestamp.minute
    if 9 * 60 + 30 <= minutes <= 11 * 60 + 30:
        return "morning"
    if 13 * 60 <= minutes <= 15 * 60:
        return "afternoon"
    return None


def _is_session_open(timestamp: datetime) -> bool:
    minutes = timestamp.hour * 60 + timestamp.minute
    return minutes in {9 * 60 + 30, 13 * 60}


def _is_five_minute_close(timestamp: datetime) -> bool:
    if _session_name(timestamp) is None or _is_session_open(timestamp):
        return False
    return timestamp.minute % 5 == 0


def _build_group(group: list[Candle]) -> Candle:
    first = group[0]
    return Candle(
        symbol=first.symbol,
        timestamp=group[-1].timestamp,
        open=first.open,
        high=max(candle.high for candle in group),
        low=min(candle.low for candle in group),
        close=group[-1].close,
        volume=sum(candle.volume for candle in group),
    )
