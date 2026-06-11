from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tmaker.domain.models import Candle, MarketQuote, TradeConfirmation, TradeConfirmationCreate
from tmaker.strategy.replay import ReplayPoint


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stock_minute_bars (
  symbol TEXT NOT NULL,
  timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  trade_date DATE NOT NULL,
  open NUMERIC NOT NULL,
  high NUMERIC NOT NULL,
  low NUMERIC NOT NULL,
  close NUMERIC NOT NULL,
  volume NUMERIC NOT NULL,
  source TEXT NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_stock_minute_bars_symbol_date
  ON stock_minute_bars (symbol, trade_date, timestamp);

CREATE TABLE IF NOT EXISTS stock_quotes (
  symbol TEXT NOT NULL,
  trade_date DATE NOT NULL,
  name TEXT NOT NULL,
  latest NUMERIC NOT NULL,
  previous_close NUMERIC NOT NULL,
  open NUMERIC NOT NULL,
  high NUMERIC NOT NULL,
  low NUMERIC NOT NULL,
  change NUMERIC NOT NULL,
  change_percent NUMERIC NOT NULL,
  source TEXT NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS t_signal_points (
  symbol TEXT NOT NULL,
  timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  trade_date DATE NOT NULL,
  action TEXT NOT NULL,
  kind TEXT NOT NULL,
  price NUMERIC NOT NULL,
  confidence NUMERIC NOT NULL,
  rule_ids JSONB NOT NULL,
  reason TEXT NOT NULL,
  risks JSONB NOT NULL,
  llm_status TEXT NOT NULL,
  llm_action TEXT,
  llm_confidence NUMERIC,
  llm_summary TEXT,
  llm_reasons JSONB NOT NULL,
  wait_for JSONB NOT NULL,
  execution_allowed BOOLEAN,
  execution_blockers JSONB NOT NULL,
  strict_mode BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, timestamp, action, strict_mode)
);

CREATE INDEX IF NOT EXISTS idx_t_signal_points_symbol_date
  ON t_signal_points (symbol, trade_date, timestamp);

CREATE TABLE IF NOT EXISTS t_trade_confirmations (
  id UUID PRIMARY KEY,
  symbol TEXT NOT NULL,
  trade_date DATE NOT NULL,
  signal_timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  signal_action TEXT NOT NULL CHECK (signal_action IN ('buy', 'sell', 'hold')),
  confirm_action TEXT NOT NULL CHECK (confirm_action IN ('buy', 'sell')),
  price NUMERIC NOT NULL CHECK (price >= 0),
  quantity INTEGER NOT NULL DEFAULT 100 CHECK (quantity > 0),
  source TEXT NOT NULL,
  reason TEXT NOT NULL,
  llm_confidence NUMERIC CHECK (llm_confidence IS NULL OR (llm_confidence >= 0 AND llm_confidence <= 1)),
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_t_trade_confirmations_date_symbol
  ON t_trade_confirmations (trade_date, symbol, signal_timestamp, created_at);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);
"""


ConnectionFactory = Callable[[str], Any]


class PostgresRepository:
    def __init__(
        self,
        database_url: str,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self.database_url = database_url
        self.connection_factory = connection_factory or _connect

    def init_schema(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA_SQL)
            connection.commit()

    def save_minute_bars(self, candles: Iterable[Candle], source: str) -> None:
        rows = [
            {
                "symbol": candle.symbol,
                "timestamp": candle.timestamp,
                "trade_date": candle.timestamp.date(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "source": source,
            }
            for candle in candles
        ]
        if not rows:
            return

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO stock_minute_bars (
                      symbol, timestamp, trade_date, open, high, low, close, volume, source
                    )
                    VALUES (
                      %(symbol)s, %(timestamp)s, %(trade_date)s, %(open)s, %(high)s,
                      %(low)s, %(close)s, %(volume)s, %(source)s
                    )
                    ON CONFLICT (symbol, timestamp) DO UPDATE SET
                      trade_date = EXCLUDED.trade_date,
                      open = EXCLUDED.open,
                      high = EXCLUDED.high,
                      low = EXCLUDED.low,
                      close = EXCLUDED.close,
                      volume = EXCLUDED.volume,
                      source = EXCLUDED.source,
                      updated_at = now()
                    """,
                    rows,
                )
            connection.commit()

    def get_minute_bars(self, symbol: str, trade_date: date) -> list[Candle]:
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT symbol, timestamp, open, high, low, close, volume
                    FROM stock_minute_bars
                    WHERE symbol = %(symbol)s AND trade_date = %(trade_date)s
                    ORDER BY timestamp
                    """,
                    {"symbol": symbol, "trade_date": trade_date},
                )
                return [_row_to_candle(row) for row in cursor.fetchall()]

    def list_trading_days(self, symbol: str) -> list[str]:
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT trade_date
                    FROM stock_minute_bars
                    WHERE symbol = %(symbol)s
                    ORDER BY trade_date
                    """,
                    {"symbol": symbol},
                )
                return [row["trade_date"].isoformat() for row in cursor.fetchall()]

    def save_quote(self, quote: MarketQuote, trade_date: date, source: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO stock_quotes (
                      symbol, trade_date, name, latest, previous_close, open, high,
                      low, change, change_percent, source
                    )
                    VALUES (
                      %(symbol)s, %(trade_date)s, %(name)s, %(latest)s, %(previous_close)s,
                      %(open)s, %(high)s, %(low)s, %(change)s, %(change_percent)s, %(source)s
                    )
                    ON CONFLICT (symbol, trade_date) DO UPDATE SET
                      name = EXCLUDED.name,
                      latest = EXCLUDED.latest,
                      previous_close = EXCLUDED.previous_close,
                      open = EXCLUDED.open,
                      high = EXCLUDED.high,
                      low = EXCLUDED.low,
                      change = EXCLUDED.change,
                      change_percent = EXCLUDED.change_percent,
                      source = EXCLUDED.source,
                      updated_at = now()
                    """,
                    {
                        "symbol": quote.symbol,
                        "trade_date": trade_date,
                        "name": quote.name,
                        "latest": quote.latest,
                        "previous_close": quote.previous_close,
                        "open": quote.open,
                        "high": quote.high,
                        "low": quote.low,
                        "change": quote.change,
                        "change_percent": quote.change_percent,
                        "source": source,
                    },
                )
            connection.commit()

    def get_quote(self, symbol: str, trade_date: date) -> MarketQuote | None:
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT symbol, name, latest, previous_close, open, high, low, change, change_percent
                    FROM stock_quotes
                    WHERE symbol = %(symbol)s AND trade_date = %(trade_date)s
                    """,
                    {"symbol": symbol, "trade_date": trade_date},
                )
                rows = cursor.fetchall()
                return _row_to_quote(rows[0]) if rows else None

    def save_replay_points(self, points: Iterable[ReplayPoint], strict: bool) -> None:
        rows = [_point_to_row(point, strict) for point in points]
        if not rows:
            return

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO t_signal_points (
                      symbol, timestamp, trade_date, action, kind, price, confidence,
                      rule_ids, reason, risks, llm_status, llm_action, llm_confidence,
                      llm_summary, llm_reasons, wait_for, execution_allowed,
                      execution_blockers, strict_mode
                    )
                    VALUES (
                      %(symbol)s, %(timestamp)s, %(trade_date)s, %(action)s, %(kind)s,
                      %(price)s, %(confidence)s, %(rule_ids)s, %(reason)s, %(risks)s,
                      %(llm_status)s, %(llm_action)s, %(llm_confidence)s, %(llm_summary)s,
                      %(llm_reasons)s, %(wait_for)s, %(execution_allowed)s,
                      %(execution_blockers)s, %(strict_mode)s
                    )
                    ON CONFLICT (symbol, timestamp, action, strict_mode) DO UPDATE SET
                      kind = EXCLUDED.kind,
                      price = EXCLUDED.price,
                      confidence = EXCLUDED.confidence,
                      rule_ids = EXCLUDED.rule_ids,
                      reason = EXCLUDED.reason,
                      risks = EXCLUDED.risks,
                      llm_status = CASE
                        WHEN EXCLUDED.llm_status = 'pending' THEN t_signal_points.llm_status
                        ELSE EXCLUDED.llm_status
                      END,
                      llm_action = CASE
                        WHEN EXCLUDED.llm_status = 'pending' THEN t_signal_points.llm_action
                        ELSE EXCLUDED.llm_action
                      END,
                      llm_confidence = CASE
                        WHEN EXCLUDED.llm_status = 'pending' THEN t_signal_points.llm_confidence
                        ELSE EXCLUDED.llm_confidence
                      END,
                      llm_summary = CASE
                        WHEN EXCLUDED.llm_status = 'pending' THEN t_signal_points.llm_summary
                        ELSE EXCLUDED.llm_summary
                      END,
                      llm_reasons = CASE
                        WHEN EXCLUDED.llm_status = 'pending' THEN t_signal_points.llm_reasons
                        ELSE EXCLUDED.llm_reasons
                      END,
                      wait_for = CASE
                        WHEN EXCLUDED.llm_status = 'pending' THEN t_signal_points.wait_for
                        ELSE EXCLUDED.wait_for
                      END,
                      execution_allowed = CASE
                        WHEN EXCLUDED.llm_status = 'pending' THEN t_signal_points.execution_allowed
                        ELSE EXCLUDED.execution_allowed
                      END,
                      execution_blockers = CASE
                        WHEN EXCLUDED.llm_status = 'pending' THEN t_signal_points.execution_blockers
                        ELSE EXCLUDED.execution_blockers
                      END,
                      updated_at = now()
                    """,
                    rows,
                )
            connection.commit()

    def replace_replay_points_for_day(
        self,
        symbol: str,
        trade_date: date,
        points: Iterable[ReplayPoint],
        strict: bool,
    ) -> None:
        point_list = list(points)
        rows = [_point_to_row(point, strict) for point in point_list]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM t_signal_points
                    WHERE symbol = %(symbol)s
                      AND trade_date = %(trade_date)s
                      AND strict_mode = %(strict_mode)s
                      AND (
                        %(point_count)s = 0
                        OR (timestamp, action) NOT IN (
                          SELECT * FROM unnest(%(timestamps)s::timestamp[], %(actions)s::text[])
                        )
                      )
                    """,
                    {
                        "symbol": symbol,
                        "trade_date": trade_date,
                        "strict_mode": strict,
                        "point_count": len(rows),
                        "timestamps": [row["timestamp"] for row in rows],
                        "actions": [row["action"] for row in rows],
                    },
                )
            connection.commit()
        self.save_replay_points(point_list, strict=strict)

    def get_replay_points(self, symbol: str, trade_date: date, strict: bool) -> list[ReplayPoint]:
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT
                      symbol, timestamp, action, kind, price, confidence, rule_ids,
                      reason, risks, llm_status, llm_action, llm_confidence, llm_summary,
                      llm_reasons, wait_for, execution_allowed, execution_blockers
                    FROM t_signal_points
                    WHERE symbol = %(symbol)s
                      AND trade_date = %(trade_date)s
                      AND strict_mode = %(strict_mode)s
                    ORDER BY timestamp
                    """,
                    {"symbol": symbol, "trade_date": trade_date, "strict_mode": strict},
                )
                return [_row_to_replay_point(row) for row in cursor.fetchall()]

    def save_trade_confirmation(self, confirmation: TradeConfirmationCreate) -> TradeConfirmation:
        row = _trade_confirmation_to_row(confirmation)
        row["id"] = str(uuid4())
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_trade_confirmations (
                      id, symbol, trade_date, signal_timestamp, signal_action, confirm_action,
                      price, quantity, source, reason, llm_confidence
                    )
                    VALUES (
                      %(id)s, %(symbol)s, %(trade_date)s, %(signal_timestamp)s,
                      %(signal_action)s, %(confirm_action)s, %(price)s, %(quantity)s,
                      %(source)s, %(reason)s, %(llm_confidence)s
                    )
                    RETURNING id::text AS id, created_at
                    """,
                    row,
                )
                saved_rows = cursor.fetchall()
            connection.commit()
        saved = saved_rows[0]
        return TradeConfirmation(
            id=saved["id"],
            created_at=saved["created_at"],
            **confirmation.model_dump(),
            trade_date=row["trade_date"],
        )

    def list_trade_confirmations(self, trade_date: date) -> list[TradeConfirmation]:
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT
                      id::text AS id, symbol, trade_date, signal_timestamp, signal_action,
                      confirm_action, price, quantity, source, reason, llm_confidence, created_at
                    FROM t_trade_confirmations
                    WHERE trade_date = %(trade_date)s
                    ORDER BY symbol, signal_timestamp, created_at
                    """,
                    {"trade_date": trade_date},
                )
                return [_row_to_trade_confirmation(row) for row in cursor.fetchall()]

    def list_trade_confirmations_range(
        self,
        start_date: date,
        end_date: date,
    ) -> list[TradeConfirmation]:
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT
                      id::text AS id, symbol, trade_date, signal_timestamp, signal_action,
                      confirm_action, price, quantity, source, reason, llm_confidence, created_at
                    FROM t_trade_confirmations
                    WHERE trade_date >= %(start_date)s
                      AND trade_date <= %(end_date)s
                    ORDER BY trade_date, symbol, signal_timestamp, created_at
                    """,
                    {"start_date": start_date, "end_date": end_date},
                )
                return [_row_to_trade_confirmation(row) for row in cursor.fetchall()]

    def delete_trade_confirmation(self, confirmation_id: str) -> bool:
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    DELETE FROM t_trade_confirmations
                    WHERE id = %(id)s
                    RETURNING id::text AS id
                    """,
                    {"id": confirmation_id},
                )
                rows = cursor.fetchall()
            connection.commit()
        return bool(rows)

    def get_bool_setting(self, key: str, default: bool) -> bool:
        with self._connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT value
                    FROM app_settings
                    WHERE key = %(key)s
                    """,
                    {"key": key},
                )
                rows = cursor.fetchall()
        if not rows:
            return default
        value = rows[0]["value"]
        return value if isinstance(value, bool) else default

    def set_bool_setting(self, key: str, value: bool) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO app_settings (key, value)
                    VALUES (%(key)s, %(value)s)
                    ON CONFLICT (key) DO UPDATE SET
                      value = EXCLUDED.value,
                      updated_at = now()
                    """,
                    {"key": key, "value": Jsonb(value)},
                )
            connection.commit()

    def _connect(self) -> Any:
        return self.connection_factory(self.database_url)


def _connect(database_url: str) -> Any:
    return psycopg.connect(database_url)


def _row_to_candle(row: dict[str, Any]) -> Candle:
    return Candle(
        symbol=row["symbol"],
        timestamp=row["timestamp"],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def _row_to_quote(row: dict[str, Any]) -> MarketQuote:
    return MarketQuote(
        symbol=row["symbol"],
        name=row["name"],
        latest=float(row["latest"]),
        previous_close=float(row["previous_close"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        change=float(row["change"]),
        change_percent=float(row["change_percent"]),
    )


def _point_to_row(point: ReplayPoint, strict: bool) -> dict[str, Any]:
    timestamp = datetime.fromisoformat(point.timestamp)
    return {
        "symbol": point.symbol,
        "timestamp": timestamp,
        "trade_date": timestamp.date(),
        "action": point.action,
        "kind": point.kind,
        "price": point.price,
        "confidence": point.confidence,
        "rule_ids": Jsonb(point.rule_ids),
        "reason": point.reason,
        "risks": Jsonb(point.risks),
        "llm_status": point.llm_status,
        "llm_action": point.llm_action,
        "llm_confidence": point.llm_confidence,
        "llm_summary": point.llm_summary,
        "llm_reasons": Jsonb(point.llm_reasons),
        "wait_for": Jsonb(point.wait_for),
        "execution_allowed": point.execution_allowed,
        "execution_blockers": Jsonb(point.execution_blockers),
        "strict_mode": strict,
    }


def _row_to_replay_point(row: dict[str, Any]) -> ReplayPoint:
    return ReplayPoint(
        symbol=row["symbol"],
        timestamp=_iso_timestamp(row["timestamp"]),
        action=row["action"],
        kind=row["kind"],
        price=float(row["price"]),
        confidence=float(row["confidence"]),
        rule_ids=list(row["rule_ids"]),
        reason=row["reason"],
        risks=list(row["risks"]),
        llm_status=row["llm_status"],
        llm_action=row["llm_action"],
        llm_confidence=float(row["llm_confidence"]) if row["llm_confidence"] is not None else None,
        llm_summary=row["llm_summary"],
        llm_reasons=list(row["llm_reasons"]),
        wait_for=list(row["wait_for"]),
        execution_allowed=row["execution_allowed"],
        execution_blockers=list(row["execution_blockers"]),
    )


def _trade_confirmation_to_row(confirmation: TradeConfirmationCreate) -> dict[str, Any]:
    return {
        "symbol": confirmation.symbol,
        "trade_date": confirmation.signal_timestamp.date(),
        "signal_timestamp": confirmation.signal_timestamp,
        "signal_action": confirmation.signal_action.value,
        "confirm_action": confirmation.confirm_action.value,
        "price": confirmation.price,
        "quantity": confirmation.quantity,
        "source": confirmation.source,
        "reason": confirmation.reason,
        "llm_confidence": confirmation.llm_confidence,
    }


def _row_to_trade_confirmation(row: dict[str, Any]) -> TradeConfirmation:
    return TradeConfirmation(
        id=row["id"],
        symbol=row["symbol"],
        trade_date=row["trade_date"],
        signal_timestamp=row["signal_timestamp"],
        signal_action=row["signal_action"],
        confirm_action=row["confirm_action"],
        price=float(row["price"]),
        quantity=int(row["quantity"]),
        source=row["source"],
        reason=row["reason"],
        llm_confidence=float(row["llm_confidence"]) if row["llm_confidence"] is not None else None,
        created_at=row["created_at"],
    )


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
