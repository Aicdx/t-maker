from __future__ import annotations

from datetime import date

from psycopg.types.json import Jsonb

from tmaker.domain.models import Candle
from tmaker.storage.postgres import PostgresRepository, SCHEMA_SQL
from tmaker.strategy.replay import ReplayPoint


def test_schema_sql_creates_market_and_signal_tables() -> None:
    assert "CREATE TABLE IF NOT EXISTS stock_minute_bars" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS stock_quotes" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS t_signal_points" in SCHEMA_SQL
    assert "PRIMARY KEY (symbol, timestamp)" in SCHEMA_SQL
    assert "PRIMARY KEY (symbol, trade_date)" in SCHEMA_SQL
    assert "PRIMARY KEY (symbol, timestamp, action, strict_mode)" in SCHEMA_SQL


def test_repository_saves_and_reads_minute_bars() -> None:
    connection = FakeConnection(
        rows=[
            [
                {
                    "symbol": "300308",
                    "timestamp": "2026-06-05T09:30:00",
                    "open": 10,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1200,
                }
            ]
        ]
    )
    repo = PostgresRepository("postgresql://example", connection_factory=lambda _: connection)
    candle = Candle(
        symbol="300308",
        timestamp="2026-06-05T09:30:00",
        open=10,
        high=10.5,
        low=9.8,
        close=10.2,
        volume=1200,
    )

    repo.save_minute_bars([candle], source="test")
    bars = repo.get_minute_bars("300308", date(2026, 6, 5))

    assert connection.executemany_calls
    assert connection.executemany_calls[0]["params"][0]["symbol"] == "300308"
    assert connection.executemany_calls[0]["params"][0]["trade_date"] == date(2026, 6, 5)
    assert bars == [candle]


def test_repository_lists_trading_days() -> None:
    connection = FakeConnection(rows=[[{"trade_date": date(2026, 6, 4)}, {"trade_date": date(2026, 6, 5)}]])
    repo = PostgresRepository("postgresql://example", connection_factory=lambda _: connection)

    days = repo.list_trading_days("300308")

    assert days == ["2026-06-04", "2026-06-05"]


def test_repository_saves_and_reads_replay_points() -> None:
    point = ReplayPoint(
        symbol="300308",
        timestamp="2026-06-05T10:05:00",
        action="buy",
        kind="candidate_buy",
        price=9.8,
        confidence=0.72,
        rule_ids=["vwap_deviation"],
        reason="低于 VWAP",
        risks=["趋势偏弱"],
        llm_status="pending",
    )
    connection = FakeConnection(
        rows=[
            [
                {
                    "symbol": "300308",
                    "timestamp": "2026-06-05T10:05:00",
                    "action": "buy",
                    "kind": "candidate_buy",
                    "price": 9.8,
                    "confidence": 0.72,
                    "rule_ids": ["vwap_deviation"],
                    "reason": "低于 VWAP",
                    "risks": ["趋势偏弱"],
                    "llm_status": "pending",
                    "llm_action": None,
                    "llm_confidence": None,
                    "llm_summary": None,
                    "llm_reasons": [],
                    "wait_for": [],
                    "execution_allowed": None,
                    "execution_blockers": [],
                }
            ]
        ]
    )
    repo = PostgresRepository("postgresql://example", connection_factory=lambda _: connection)

    repo.save_replay_points([point], strict=True)
    points = repo.get_replay_points("300308", date(2026, 6, 5), strict=True)

    assert connection.executemany_calls
    saved = connection.executemany_calls[0]["params"][0]
    assert saved["symbol"] == "300308"
    assert saved["strict_mode"] is True
    assert saved["trade_date"] == date(2026, 6, 5)
    assert isinstance(saved["rule_ids"], Jsonb)
    assert isinstance(saved["risks"], Jsonb)
    assert isinstance(saved["llm_reasons"], Jsonb)
    assert isinstance(saved["wait_for"], Jsonb)
    assert isinstance(saved["execution_blockers"], Jsonb)
    assert points == [point]


def test_repository_does_not_overwrite_review_with_pending_replay_point() -> None:
    point = ReplayPoint(
        symbol="300308",
        timestamp="2026-06-05T10:05:00",
        action="sell",
        kind="candidate_sell",
        price=131.5,
        confidence=0.68,
        rule_ids=["intraday_gain_session_vwap_stretch"],
        reason="强势冲高",
        risks=["可能继续上冲"],
        llm_status="pending",
    )
    connection = FakeConnection()
    repo = PostgresRepository("postgresql://example", connection_factory=lambda _: connection)

    repo.save_replay_points([point], strict=True)

    sql = connection.executemany_calls[0]["sql"]
    assert "WHEN EXCLUDED.llm_status = 'pending'" in sql
    assert "THEN t_signal_points.llm_status" in sql
    assert "THEN t_signal_points.llm_action" in sql
    assert "THEN t_signal_points.llm_summary" in sql


def test_repository_replaces_replay_points_for_symbol_day() -> None:
    point = ReplayPoint(
        symbol="300308",
        timestamp="2026-06-05T10:05:00",
        action="sell",
        kind="candidate_sell",
        price=131.5,
        confidence=0.68,
        rule_ids=["intraday_gain_session_vwap_stretch"],
        reason="强势冲高",
        risks=["可能继续上冲"],
        llm_status="pending",
    )
    connection = FakeConnection()
    repo = PostgresRepository("postgresql://example", connection_factory=lambda _: connection)

    repo.replace_replay_points_for_day("300308", date(2026, 6, 5), [point], strict=True)

    assert connection.execute_calls
    delete_call = connection.execute_calls[0]
    assert "DELETE FROM t_signal_points" in delete_call["sql"]
    assert delete_call["params"]["symbol"] == "300308"
    assert delete_call["params"]["trade_date"] == date(2026, 6, 5)
    assert connection.executemany_calls


class FakeConnection:
    def __init__(self, rows: list[list[dict]] | None = None) -> None:
        self.rows = rows or []
        self.execute_calls: list[dict] = []
        self.executemany_calls: list[dict] = []

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self, **_kwargs: object) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        return None


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.current_rows: list[dict] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object | None = None) -> None:
        self.connection.execute_calls.append({"sql": sql, "params": params})
        if sql.lstrip().lower().startswith("select") and self.connection.rows:
            self.current_rows = self.connection.rows.pop(0)

    def executemany(self, sql: str, params_seq: list[dict]) -> None:
        self.connection.executemany_calls.append({"sql": sql, "params": params_seq})

    def fetchall(self) -> list[dict]:
        return self.current_rows
