import asyncio
from datetime import date, datetime
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest

import tmaker.api.app as app_module
from tmaker.api.app import create_app
from tmaker.domain.models import Candle, MarketQuote, TradeConfirmation, TradeConfirmationCreate
from tmaker.market.akshare_provider import MarketDataChannelUnavailable, MarketDataUnavailable
from tmaker.notify.feishu import FeishuConfigError, FeishuDeliveryError
from tmaker.strategy.replay import ReplayPoint


class FakeProvider:
    def __init__(
        self,
        candles: list[Candle] | dict[str, list[Candle]] | Exception,
        quotes: dict[str, MarketQuote] | Exception | None = None,
    ) -> None:
        self.candles = candles
        self.quotes = quotes
        self.calls: list[str] = []
        self.day_calls: list[tuple[str, date]] = []
        self.quote_calls: list[str] = []

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        self.calls.append(symbol)
        if isinstance(self.candles, Exception):
            raise self.candles
        if isinstance(self.candles, dict):
            return self.candles.get(symbol, _provider_candles(symbol))
        return self.candles

    def fetch_minutes_for_date(self, symbol: str, trade_date: date) -> list[Candle]:
        self.day_calls.append((symbol, trade_date))
        candles = self.fetch_minutes(symbol)
        return [candle for candle in candles if candle.timestamp.date() == trade_date]

    def fetch_quote(self, symbol: str) -> MarketQuote:
        self.quote_calls.append(symbol)
        if isinstance(self.quotes, Exception):
            raise self.quotes
        if isinstance(self.quotes, dict):
            return self.quotes[symbol]
        raise RuntimeError("quote unavailable")


class RepairingProvider(FakeProvider):
    def __init__(self, repair_candles: list[Candle]) -> None:
        super().__init__(repair_candles)
        self.fetch_for_date_calls: list[tuple[str, date]] = []

    def fetch_minutes_for_date(self, symbol: str, trade_date: date) -> list[Candle]:
        self.fetch_for_date_calls.append((symbol, trade_date))
        return [
            candle
            for candle in self.candles
            if isinstance(self.candles, list)
            and candle.symbol == symbol
            and candle.timestamp.date() == trade_date
        ]


class ProgressiveProvider(FakeProvider):
    def __init__(self, candles: dict[str, list[list[Candle]]]) -> None:
        super().__init__({})
        self.progressive_candles = candles
        self.call_counts: dict[str, int] = {}

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        self.calls.append(symbol)
        snapshots = self.progressive_candles[symbol]
        call_count = self.call_counts.get(symbol, 0)
        self.call_counts[symbol] = call_count + 1
        return snapshots[min(call_count, len(snapshots) - 1)]


class FakeReviewClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_review(self, context: dict) -> dict:
        self.calls.append(context)
        return {
            "action": "buy",
            "confidence": 0.64,
            "summary": "模型确认可作为低吸观察点",
            "reasons": ["价格低于 VWAP", "短线量能收缩"],
            "risks": ["趋势仍弱"],
            "wait_for": ["下一根 1 分钟 K 线不破新低"],
        }

class BlockingReviewClient(FakeReviewClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    async def create_review(self, context: dict) -> dict:
        self.calls.append(context)
        self.started.set()
        await asyncio.to_thread(self.release.wait)
        return {
            "action": "buy",
            "confidence": 0.64,
            "summary": "模型确认可作为低吸观察点",
            "reasons": ["价格低于 VWAP", "短线量能收缩"],
            "risks": ["趋势仍弱"],
            "wait_for": ["下一根 1 分钟 K 线不破新低"],
        }


class FakeMonitorAnalyzer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def analyze(self, context: dict) -> dict:
        self.calls.append(context)
        return {
            "judgement": "wait",
            "summary": "fake monitor analysis",
            "key_levels": [],
            "next_steps": [],
            "invalidates": [],
            "risk_notes": [],
        }


class FakeMonitorNotifier:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.messages: list[str] = []

    async def send_text(self, text: str) -> None:
        self.messages.append(text)
        if self.exc is not None:
            raise self.exc

    def send_text_sync(self, text: str) -> None:
        self.messages.append(text)
        if self.exc is not None:
            raise self.exc


class FakeRepository:
    def __init__(self) -> None:
        self.minute_bars: dict[tuple[str, date], list[Candle]] = {}
        self.points: dict[tuple[str, date, bool], list[ReplayPoint]] = {}
        self.quotes: dict[tuple[str, date], MarketQuote] = {}
        self.confirmations: dict[str, TradeConfirmation] = {}
        self.bool_settings: dict[str, bool] = {}
        self.saved_bars: list[Candle] = []
        self.saved_points: list[ReplayPoint] = []
        self.schema_initialized = False

    def init_schema(self) -> None:
        self.schema_initialized = True

    def save_minute_bars(self, candles: list[Candle], source: str) -> None:
        self.saved_bars.extend(candles)
        for candle in candles:
            key = (candle.symbol, candle.timestamp.date())
            self.minute_bars.setdefault(key, [])
            self.minute_bars[key] = [
                existing for existing in self.minute_bars[key] if existing.timestamp != candle.timestamp
            ]
            self.minute_bars[key].append(candle)
            self.minute_bars[key].sort(key=lambda item: item.timestamp)

    def get_minute_bars(self, symbol: str, trade_date: date) -> list[Candle]:
        return self.minute_bars.get((symbol, trade_date), [])

    def list_trading_days(self, symbol: str) -> list[str]:
        return sorted(
            trade_date.isoformat()
            for stored_symbol, trade_date in self.minute_bars
            if stored_symbol == symbol
        )

    def save_quote(self, quote: MarketQuote, trade_date: date, source: str) -> None:
        self.quotes[(quote.symbol, trade_date)] = quote

    def get_quote(self, symbol: str, trade_date: date) -> MarketQuote | None:
        return self.quotes.get((symbol, trade_date))

    def save_replay_points(self, points: list[ReplayPoint], strict: bool) -> None:
        self.saved_points.extend(points)
        for point in points:
            trade_date = datetime.fromisoformat(point.timestamp).date()
            key = (point.symbol, trade_date, strict)
            self.points.setdefault(key, [])
            self.points[key] = [existing for existing in self.points[key] if existing.timestamp != point.timestamp]
            self.points[key].append(point)
            self.points[key].sort(key=lambda item: item.timestamp)

    def replace_replay_points_for_day(
        self,
        symbol: str,
        trade_date: date,
        points: list[ReplayPoint],
        strict: bool,
    ) -> None:
        self.points[(symbol, trade_date, strict)] = []
        self.save_replay_points(points, strict)

    def get_replay_points(self, symbol: str, trade_date: date, strict: bool) -> list[ReplayPoint]:
        return self.points.get((symbol, trade_date, strict), [])

    def save_trade_confirmation(self, confirmation: TradeConfirmationCreate) -> TradeConfirmation:
        confirmation_id = f"confirm-{len(self.confirmations) + 1}"
        saved = TradeConfirmation(
            id=confirmation_id,
            trade_date=confirmation.signal_timestamp.date(),
            created_at=confirmation.signal_timestamp,
            **confirmation.model_dump(),
        )
        self.confirmations[confirmation_id] = saved
        return saved

    def list_trade_confirmations(self, trade_date: date) -> list[TradeConfirmation]:
        return [
            confirmation
            for confirmation in self.confirmations.values()
            if confirmation.trade_date == trade_date
        ]

    def list_trade_confirmations_range(
        self,
        start_date: date,
        end_date: date,
    ) -> list[TradeConfirmation]:
        return [
            confirmation
            for confirmation in self.confirmations.values()
            if start_date <= confirmation.trade_date <= end_date
        ]

    def delete_trade_confirmation(self, confirmation_id: str) -> bool:
        return self.confirmations.pop(confirmation_id, None) is not None

    def get_bool_setting(self, key: str, default: bool) -> bool:
        return self.bool_settings.get(key, default)

    def set_bool_setting(self, key: str, value: bool) -> None:
        self.bool_settings[key] = value


def test_health_endpoint_reports_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def _test_app(**kwargs):
    kwargs.setdefault("repository", FakeRepository())
    return create_app(**kwargs)


def _eventually(predicate, timeout: float = 2, interval: float = 0.02) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_snapshot_endpoint_returns_watchlist_positions_and_signals() -> None:
    client = TestClient(_test_app(minute_provider=FakeProvider(_provider_candles())))

    response = client.get("/api/snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert [(item["symbol"], item["name"]) for item in payload["watchlist"]] == [
        ("300308", "中际旭创"),
        ("300502", "新易盛"),
        ("600487", "亨通光电"),
        ("000636", "风华高科"),
    ]
    assert {position["symbol"] for position in payload["positions"]} == {
        "000636",
        "300308",
        "300502",
        "600487",
    }
    positions_by_symbol = {position["symbol"]: position for position in payload["positions"]}
    assert positions_by_symbol["000636"]["base_quantity"] == 500
    assert positions_by_symbol["300308"]["base_quantity"] == 200
    assert positions_by_symbol["300502"]["base_quantity"] == 200
    assert positions_by_symbol["000636"]["available_cash"] == 200000
    assert positions_by_symbol["300308"]["available_cash"] == 200000
    assert positions_by_symbol["300502"]["available_cash"] == 200000
    assert positions_by_symbol["600487"]["available_cash"] == 200000
    assert "signals" in payload


def test_simulate_tick_adds_a_signal() -> None:
    client = TestClient(create_app())

    response = client.post("/api/simulate/tick")

    assert response.status_code == 200
    payload = response.json()
    assert payload["signals"]
    assert payload["signals"][-1]["kind"] in ["candidate_buy", "candidate_sell", "hold"]


def test_snapshot_uses_injected_real_provider_when_available() -> None:
    candles = {
        "000636": _provider_candles("000636", close=14.2),
        "600487": _provider_candles("600487", close=23.4),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    provider = FakeProvider(candles)
    client = TestClient(_test_app(minute_provider=provider))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert provider.calls == ["300308", "300502", "600487", "000636"]
    assert payload["provider_health"]["provider"] == "tencent_ifzq"
    assert payload["provider_health"]["last_success_at"] == "2026-06-05T09:32:00"
    assert {candle["symbol"] for candle in payload["candles"]} == {
        "000636",
        "300308",
        "300502",
        "600487",
    }
    closes = {candle["symbol"]: candle["close"] for candle in payload["candles"]}
    assert closes["000636"] == 14.2
    assert closes["600487"] == 23.4


def test_snapshot_returns_realtime_quotes_when_available() -> None:
    candles = {
        "000636": _provider_candles("000636", close=14.2),
        "600487": _provider_candles("600487", close=23.4),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    quotes = {
        "000636": _quote("000636", "风华高科", latest=14.3, previous_close=14.1, open_price=14.12),
        "600487": _quote("600487", "亨通光电", latest=23.8, previous_close=23.2, open_price=23.3),
        "300308": _quote("300308", "中际旭创", latest=1179.99, previous_close=1280, open_price=1273.2),
        "300502": _quote("300502", "新易盛", latest=748, previous_close=775.94, open_price=790),
    }
    provider = FakeProvider(candles, quotes=quotes)
    client = TestClient(_test_app(minute_provider=provider))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert provider.quote_calls == ["300308", "300502", "600487", "000636"]
    assert payload["quotes"]["300308"] == {
        "symbol": "300308",
        "name": "中际旭创",
        "latest": 1179.99,
        "previous_close": 1280.0,
        "open": 1273.2,
        "high": 1301.51,
        "low": 1160.0,
        "change": -100.01,
        "change_percent": -7.81,
    }


def test_snapshot_keeps_intraday_candles_for_each_watch_symbol() -> None:
    candles = {
        "000636": _many_provider_candles("000636", close=14.2),
        "600487": _many_provider_candles("600487", close=23.4),
        "300308": _many_provider_candles("300308", close=151.6),
        "300502": _many_provider_candles("300502", close=126.8),
    }
    client = TestClient(_test_app(minute_provider=FakeProvider(candles)))

    response = client.get("/api/snapshot")

    payload = response.json()
    candles_by_symbol = {
        symbol: [candle for candle in payload["candles"] if candle["symbol"] == symbol]
        for symbol in ["000636", "300308", "300502", "600487"]
    }
    assert {symbol: len(candles) for symbol, candles in candles_by_symbol.items()} == {
        "000636": 100,
        "300308": 100,
        "300502": 100,
        "600487": 100,
    }
    assert candles_by_symbol["000636"][-1]["close"] == 14.2
    assert candles_by_symbol["600487"][-1]["close"] == 23.4
    assert candles_by_symbol["300308"][-1]["close"] == 151.6
    assert candles_by_symbol["300502"][-1]["close"] == 126.8


def test_snapshot_returns_realtime_one_minute_and_five_minute_series() -> None:
    candles = {
        "000636": _session_provider_candles("000636", close=14.2),
        "600487": _session_provider_candles("600487", close=23.4),
        "300308": _session_provider_candles("300308", close=151.6),
        "300502": _session_provider_candles("300502", close=126.8),
    }
    client = TestClient(_test_app(minute_provider=FakeProvider(candles)))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert set(payload["chart_series"]) == {"realtime", "one_minute", "five_minute"}
    assert {candle["symbol"] for candle in payload["chart_series"]["realtime"]} == {
        "000636",
        "300308",
        "300502",
        "600487",
    }
    assert {candle["symbol"] for candle in payload["chart_series"]["one_minute"]} == {
        "000636",
        "300308",
        "300502",
        "600487",
    }
    five_minute_by_symbol = {
        symbol: [candle for candle in payload["chart_series"]["five_minute"] if candle["symbol"] == symbol]
        for symbol in ["000636", "300308", "300502", "600487"]
    }
    assert {symbol: len(candles) for symbol, candles in five_minute_by_symbol.items()} == {
        "000636": 2,
        "300308": 2,
        "300502": 2,
        "600487": 2,
    }
    assert five_minute_by_symbol["600487"][-1]["timestamp"] == "2026-06-05T09:40:00"


def test_snapshot_realtime_series_keeps_full_trading_day() -> None:
    candles = {
        "000636": _full_trading_day_provider_candles("000636", close=14.2),
        "600487": _full_trading_day_provider_candles("600487", close=23.4),
        "300308": _full_trading_day_provider_candles("300308", close=151.6),
        "300502": _full_trading_day_provider_candles("300502", close=126.8),
    }
    client = TestClient(_test_app(minute_provider=FakeProvider(candles)))

    response = client.get("/api/snapshot")

    payload = response.json()
    realtime = [candle for candle in payload["chart_series"]["realtime"] if candle["symbol"] == "600487"]
    one_minute = [candle for candle in payload["chart_series"]["one_minute"] if candle["symbol"] == "600487"]
    assert len(realtime) == 242
    assert realtime[0]["timestamp"] == "2026-06-05T09:30:00"
    assert realtime[-1]["timestamp"] == "2026-06-05T15:00:00"
    assert len(one_minute) == 242
    assert one_minute[0]["timestamp"] == "2026-06-05T09:30:00"
    assert one_minute[-1]["timestamp"] == "2026-06-05T15:00:00"


def test_snapshot_persists_realtime_minute_bars_for_day_review_cache() -> None:
    repository = FakeRepository()
    candles = {
        "000636": _full_trading_day_provider_candles("000636", close=14.2),
        "600487": _full_trading_day_provider_candles("600487", close=23.4),
        "300308": _full_trading_day_provider_candles("300308", close=151.6),
        "300502": _full_trading_day_provider_candles("300502", close=126.8),
    }
    client = TestClient(create_app(minute_provider=FakeProvider(candles), repository=repository))

    response = client.get("/api/snapshot")

    assert response.status_code == 200
    cached = repository.get_minute_bars("300308", date(2026, 6, 5))
    assert len(cached) == 242
    assert cached[0].timestamp == datetime(2026, 6, 5, 9, 30)
    assert cached[-1].timestamp == datetime(2026, 6, 5, 15, 0)


def test_snapshot_falls_back_to_demo_data_when_provider_fails() -> None:
    provider = FakeProvider(MarketDataUnavailable("empty"))
    client = TestClient(_test_app(minute_provider=provider))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert response.status_code == 200
    assert payload["provider_health"]["provider"] == "tencent_ifzq_fallback"
    assert payload["provider_health"]["missing_candle_count"] >= 1
    assert payload["provider_health"]["last_error"] == (
        "300308: empty；300502: empty；600487: empty；000636: empty"
    )
    assert payload["candles"]


def test_monitor_status_endpoint_reports_state() -> None:
    client = TestClient(
        _test_app(
            minute_provider=FakeProvider(_provider_candles()),
            monitor_analyzer=FakeMonitorAnalyzer(),
            monitor_notifier=FakeMonitorNotifier(),
        )
    )

    response = client.get("/api/monitor/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is False
    assert payload["notification_count"] == 0


def test_monitor_start_and_stop_are_idempotent() -> None:
    client = TestClient(
        _test_app(
            minute_provider=FakeProvider(_provider_candles()),
            monitor_analyzer=FakeMonitorAnalyzer(),
            monitor_notifier=FakeMonitorNotifier(),
        )
    )

    start_response = client.post("/api/monitor/start")
    second_start_response = client.post("/api/monitor/start")
    stop_response = client.post("/api/monitor/stop")
    second_stop_response = client.post("/api/monitor/stop")

    assert start_response.status_code == 200
    assert second_start_response.status_code == 200
    assert stop_response.status_code == 200
    assert second_stop_response.status_code == 200
    assert second_stop_response.json()["running"] is False


def test_monitor_test_feishu_reports_missing_webhook() -> None:
    client = TestClient(
        _test_app(
            minute_provider=FakeProvider(_provider_candles()),
            monitor_analyzer=FakeMonitorAnalyzer(),
            monitor_notifier=FakeMonitorNotifier(FeishuConfigError("FEISHU_WEBHOOK_URL is not configured")),
        )
    )

    response = client.post("/api/monitor/test-feishu")

    assert response.status_code == 503
    assert "FEISHU_WEBHOOK_URL" in response.json()["detail"]


def test_monitor_test_feishu_maps_delivery_error_to_bad_gateway() -> None:
    client = TestClient(
        _test_app(
            minute_provider=FakeProvider(_provider_candles()),
            monitor_analyzer=FakeMonitorAnalyzer(),
            monitor_notifier=FakeMonitorNotifier(FeishuDeliveryError("bad webhook")),
        )
    )

    response = client.post("/api/monitor/test-feishu")

    assert response.status_code == 502
    assert "bad webhook" in response.json()["detail"]


def test_monitor_test_feishu_maps_network_error_to_bad_gateway() -> None:
    client = TestClient(
        _test_app(
            minute_provider=FakeProvider(_provider_candles()),
            monitor_analyzer=FakeMonitorAnalyzer(),
            monitor_notifier=FakeMonitorNotifier(httpx.ConnectError("network")),
        )
    )

    response = client.post("/api/monitor/test-feishu")

    assert response.status_code == 502
    assert "network" in response.json()["detail"]


def test_notification_settings_api_reads_and_updates_feishu_flag() -> None:
    repository = FakeRepository()
    client = TestClient(_test_app(repository=repository))

    initial = client.get("/api/settings/notifications")
    updated = client.put(
        "/api/settings/notifications",
        json={"feishu_notifications_enabled": False, "review_day_feishu_enabled": True},
    )
    refreshed = client.get("/api/settings/notifications")

    assert initial.status_code == 200
    assert initial.json() == {
        "feishu_notifications_enabled": True,
        "review_day_feishu_enabled": False,
    }
    assert updated.status_code == 200
    assert updated.json() == {
        "feishu_notifications_enabled": False,
        "review_day_feishu_enabled": True,
    }
    assert refreshed.json() == {
        "feishu_notifications_enabled": False,
        "review_day_feishu_enabled": True,
    }
    assert repository.bool_settings["feishu_notifications_enabled"] is False
    assert repository.bool_settings["review_day_feishu_enabled"] is True


def test_trade_confirmation_api_saves_selected_ai_point() -> None:
    repository = FakeRepository()
    client = TestClient(create_app(repository=repository))

    response = client.post(
        "/api/trade-confirmations",
        json={
            "symbol": "300308",
            "signal_timestamp": "2026-06-10T10:24:00",
            "signal_action": "buy",
            "confirm_action": "buy",
            "price": 123.45,
            "quantity": 100,
            "source": "monitor",
            "reason": "AI低吸点位",
            "llm_confidence": 0.72,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "confirm-1"
    assert payload["trade_date"] == "2026-06-10"
    assert repository.confirmations["confirm-1"].confirm_action == "buy"


def test_trade_confirmation_stats_defaults_to_today(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository()
    client = TestClient(create_app(repository=repository))
    monkeypatch.setattr(app_module, "_today", lambda: date(2026, 6, 10))
    client.post(
        "/api/trade-confirmations",
        json={
            "symbol": "300308",
            "signal_timestamp": "2026-06-10T10:24:00",
            "signal_action": "buy",
            "confirm_action": "buy",
            "price": 123.4,
            "source": "monitor",
            "reason": "AI低吸点位",
        },
    )
    client.post(
        "/api/trade-confirmations",
        json={
            "symbol": "300308",
            "signal_timestamp": "2026-06-10T13:12:00",
            "signal_action": "sell",
            "confirm_action": "sell",
            "price": 125.1,
            "source": "monitor",
            "reason": "AI高抛点位",
        },
    )

    response = client.get("/api/trade-confirmations/stats")

    assert response.status_code == 200
    payload = response.json()
    assert payload["date"] == "2026-06-10"
    assert payload["summary"]["paired_count"] == 1
    assert payload["summary"]["total_pnl"] == 170.0


def test_trade_confirmation_stats_honors_date_query(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository()
    client = TestClient(create_app(repository=repository))
    monkeypatch.setattr(app_module, "_today", lambda: date(2026, 6, 10))
    client.post(
        "/api/trade-confirmations",
        json={
            "symbol": "300308",
            "signal_timestamp": "2026-06-09T10:24:00",
            "signal_action": "buy",
            "confirm_action": "buy",
            "price": 123.4,
            "source": "monitor",
            "reason": "AI低吸点位",
        },
    )

    today_response = client.get("/api/trade-confirmations/stats")
    selected_response = client.get("/api/trade-confirmations/stats?date=2026-06-09")

    assert today_response.json()["summary"]["record_count"] == 0
    assert selected_response.json()["summary"]["record_count"] == 1


def test_trade_confirmation_stats_filters_current_symbol() -> None:
    repository = FakeRepository()
    client = TestClient(_test_app(repository=repository))
    for symbol, price in [("300308", 123.4), ("300308", 125.1), ("600487", 28.3)]:
        client.post(
            "/api/trade-confirmations",
            json={
                "symbol": symbol,
                "signal_timestamp": "2026-06-10T10:24:00",
                "signal_action": "buy" if price < 125 else "sell",
                "confirm_action": "buy" if price < 125 else "sell",
                "price": price,
                "source": "monitor",
                "reason": "AI点位",
            },
        )

    response = client.get("/api/trade-confirmations/stats?date=2026-06-10&symbol=300308")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["record_count"] == 2
    assert payload["summary"]["paired_count"] == 1
    assert [pair["symbol"] for pair in payload["pairs"]] == ["300308"]
    assert payload["unpaired"] == []


def test_trade_confirmation_summary_groups_by_date_and_symbol() -> None:
    repository = FakeRepository()
    client = TestClient(_test_app(repository=repository))
    records = [
        ("300308", "2026-06-09T10:24:00", "buy", 100.0),
        ("300308", "2026-06-09T13:12:00", "sell", 101.0),
        ("600487", "2026-06-10T10:24:00", "buy", 20.0),
        ("600487", "2026-06-10T14:06:00", "sell", 21.5),
        ("300308", "2026-06-10T10:30:00", "buy", 102.0),
    ]
    for symbol, timestamp, action, price in records:
        client.post(
            "/api/trade-confirmations",
            json={
                "symbol": symbol,
                "signal_timestamp": timestamp,
                "signal_action": action,
                "confirm_action": action,
                "price": price,
                "source": "monitor",
                "reason": "AI点位",
            },
        )

    response = client.get("/api/trade-confirmations/summary?start_date=2026-06-09&end_date=2026-06-10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["start_date"] == "2026-06-09"
    assert payload["end_date"] == "2026-06-10"
    assert payload["summary"]["record_count"] == 5
    assert payload["summary"]["paired_count"] == 2
    assert payload["summary"]["unpaired_count"] == 1
    assert payload["summary"]["total_pnl"] == 250.0
    assert [(row["date"], row["summary"]["total_pnl"]) for row in payload["by_date"]] == [
        ("2026-06-09", 100.0),
        ("2026-06-10", 150.0),
    ]
    assert [(row["symbol"], row["summary"]["record_count"]) for row in payload["by_symbol"]] == [
        ("300308", 3),
        ("600487", 2),
    ]


def test_trade_confirmation_delete_removes_record_or_returns_404() -> None:
    repository = FakeRepository()
    client = TestClient(create_app(repository=repository))
    created = client.post(
        "/api/trade-confirmations",
        json={
            "symbol": "300308",
            "signal_timestamp": "2026-06-10T10:24:00",
            "signal_action": "buy",
            "confirm_action": "buy",
            "price": 123.4,
            "source": "monitor",
            "reason": "AI低吸点位",
        },
    ).json()

    delete_response = client.delete(f"/api/trade-confirmations/{created['id']}")
    missing_response = client.delete(f"/api/trade-confirmations/{created['id']}")

    assert delete_response.status_code == 200
    assert delete_response.json() == {"status": "ok"}
    assert missing_response.status_code == 404


def _provider_candles(symbol: str = "300308", close: float = 151.6) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timestamp="2026-06-05T09:31:00",
            open=close - 0.2,
            high=close,
            low=close - 0.3,
            close=close - 0.1,
            volume=1000,
        ),
        Candle(
            symbol=symbol,
            timestamp="2026-06-05T09:32:00",
            open=close - 0.1,
            high=close + 0.1,
            low=close - 0.2,
            close=close,
            volume=1200,
        ),
    ]


def _quote(symbol: str, name: str, latest: float, previous_close: float, open_price: float) -> MarketQuote:
    change = round(latest - previous_close, 2)
    return MarketQuote(
        symbol=symbol,
        name=name,
        latest=latest,
        previous_close=previous_close,
        open=open_price,
        high=1301.51 if symbol == "300308" else max(latest, open_price),
        low=1160 if symbol == "300308" else min(latest, open_price),
        change=change,
        change_percent=round(change / previous_close * 100, 2),
    )


def _many_provider_candles(symbol: str, close: float) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T{9 + ((30 + index) // 60):02d}:{(30 + index) % 60:02d}:00",
            open=close - 0.1,
            high=close + 0.1,
            low=close - 0.2,
            close=close,
            volume=1000 + index,
        )
        for index in range(100)
    ]


def _session_provider_candles(symbol: str, close: float) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T09:{30 + index:02d}:00",
            open=close + index * 0.01,
            high=close + index * 0.01 + 0.1,
            low=close + index * 0.01 - 0.1,
            close=close + index * 0.01,
            volume=1000 + index,
        )
        for index in range(11)
    ]


def _session_provider_candles_on(symbol: str, trade_date: date, close: float) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timestamp=f"{trade_date.isoformat()}T09:{30 + index:02d}:00",
            open=close + index * 0.01,
            high=close + index * 0.01 + 0.1,
            low=close + index * 0.01 - 0.1,
            close=close + index * 0.01,
            volume=1000 + index,
        )
        for index in range(11)
    ]


def _full_trading_day_provider_candles(symbol: str, close: float) -> list[Candle]:
    minutes = [
        *(f"{hour:02d}:{minute:02d}" for hour in range(9, 12) for minute in range(60)),
        *(f"{hour:02d}:{minute:02d}" for hour in range(13, 16) for minute in range(60)),
    ]
    trading_minutes = [
        minute
        for minute in minutes
        if ("09:30" <= minute <= "11:30") or ("13:00" <= minute <= "15:00")
    ]
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T{minute}:00",
            open=close + index * 0.01,
            high=close + index * 0.01 + 0.1,
            low=close + index * 0.01 - 0.1,
            close=close + index * 0.01,
            volume=1000 + index,
        )
        for index, minute in enumerate(trading_minutes)
    ]


def test_snapshot_falls_back_when_provider_raises_external_exception() -> None:
    provider = FakeProvider(ConnectionError("proxy unavailable"))
    client = TestClient(_test_app(minute_provider=provider))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert response.status_code == 200
    assert payload["provider_health"]["provider"] == "tencent_ifzq_fallback"
    assert payload["provider_health"]["last_error"] == (
        "300308: proxy unavailable；300502: proxy unavailable；600487: proxy unavailable；000636: proxy unavailable"
    )


def test_snapshot_reviews_candidate_signals_with_llm_client() -> None:
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(
        _test_app(minute_provider=FakeProvider(candles), review_client=review_client)
    )

    first = client.get("/api/snapshot")

    payload = first.json()
    buy_signal = [
        signal
        for signal in payload["signals"]
        if signal["symbol"] == "600487" and signal["kind"] == "candidate_buy"
    ][-1]
    assert buy_signal["llm_status"] == "pending"
    assert _eventually(lambda: bool(review_client.calls))
    assert review_client.calls
    assert review_client.calls[0]["symbol"] == "600487"
    response = client.get("/api/snapshot")
    payload = response.json()
    buy_signal = [
        signal
        for signal in payload["signals"]
        if signal["symbol"] == "600487" and signal["kind"] == "candidate_buy"
    ][-1]
    assert buy_signal["llm_status"] == "ok"
    assert buy_signal["llm_review"]["summary"] == "模型确认可作为低吸观察点"


def test_snapshot_persists_reviewed_realtime_candidate_points() -> None:
    repository = FakeRepository()
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(
        create_app(
            minute_provider=FakeProvider(candles),
            review_client=review_client,
            repository=repository,
        )
    )

    response = client.get("/api/snapshot")

    assert response.status_code == 200
    assert _eventually(
        lambda: any(
            point.symbol == "600487" and point.kind == "candidate_buy" for point in repository.saved_points
        )
    )
    saved = [
        point
        for point in repository.saved_points
        if point.symbol == "600487" and point.kind == "candidate_buy"
    ]
    assert saved
    assert saved[-1].timestamp == "2026-06-05T10:05:00"
    assert saved[-1].price == 9.55
    assert saved[-1].llm_status == "ok"
    assert saved[-1].llm_action == "buy"
    assert saved[-1].llm_confidence == 0.64


def test_snapshot_returns_pending_candidate_without_waiting_for_ai_review() -> None:
    repository = FakeRepository()
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = BlockingReviewClient()
    client = TestClient(
        create_app(
            minute_provider=FakeProvider(candles),
            review_client=review_client,
            repository=repository,
        )
    )
    result: dict[str, object] = {}

    def request_snapshot() -> None:
        result["response"] = client.get("/api/snapshot")

    thread = threading.Thread(target=request_snapshot)
    thread.start()
    time.sleep(0.2)

    try:
        assert not thread.is_alive()
        assert review_client.started.is_set()
        response = result["response"]
        assert response.status_code == 200
        pending_signal = [
            signal
            for signal in response.json()["signals"]
            if signal["symbol"] == "600487" and signal["kind"] == "candidate_buy"
        ][-1]
        assert pending_signal["llm_status"] == "pending"
        assert repository.saved_points == []
    finally:
        review_client.release.set()
        thread.join(timeout=2)


def test_snapshot_hydrates_saved_realtime_points_after_backend_restart() -> None:
    repository = FakeRepository()
    repository.save_replay_points(
        [
            ReplayPoint(
                symbol="600487",
                timestamp="2026-06-05T10:05:00",
                action="buy",
                kind="candidate_buy",
                price=9.55,
                confidence=0.64,
                rule_ids=["vwap_deviation"],
                reason="早盘实时盯盘低吸候选",
                risks=["趋势仍弱"],
                llm_status="ok",
                llm_action="buy",
                llm_confidence=0.64,
                llm_summary="模型确认可作为低吸观察点",
                llm_reasons=["价格低于 VWAP"],
                wait_for=["下一根 1 分钟 K 线不破新低"],
                execution_allowed=True,
            )
        ],
        strict=True,
    )
    repository.saved_points.clear()
    candles = {
        "600487": _flat_hold_candles_after_candidate_time("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    client = TestClient(create_app(minute_provider=FakeProvider(candles), repository=repository))

    response = client.get("/api/snapshot")

    assert response.status_code == 200
    saved_signal = [
        signal
        for signal in response.json()["signals"]
        if signal["symbol"] == "600487" and signal["timestamp"] == "2026-06-05T10:05:00"
    ][-1]
    assert saved_signal["kind"] == "candidate_buy"
    assert saved_signal["llm_status"] == "ok"
    assert saved_signal["llm_review"]["summary"] == "模型确认可作为低吸观察点"
    assert repository.saved_points == []


def test_snapshot_uses_saved_realtime_points_to_merge_clusters_after_backend_restart() -> None:
    repository = FakeRepository()
    repository.save_replay_points(
        [
            ReplayPoint(
                symbol="600487",
                timestamp="2026-06-05T10:10:00",
                action="buy",
                kind="suspected",
                price=102.25,
                confidence=0.64,
                rule_ids=["pullback_low_rebound"],
                reason="早盘实时盯盘低吸候选",
                risks=["仍处于 VWAP 下方"],
                llm_status="ok",
                llm_action="buy",
                llm_confidence=0.64,
                llm_summary="模型确认可作为低吸观察点",
                llm_reasons=["价格低于 VWAP"],
                wait_for=[],
                execution_allowed=True,
            )
        ],
        strict=True,
    )
    repository.saved_points.clear()
    candles = {
        "600487": _pullback_low_rebound_same_cluster_candles("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(
        create_app(
            minute_provider=FakeProvider(candles),
            review_client=review_client,
            repository=repository,
        )
    )

    response = client.get("/api/snapshot")

    assert response.status_code == 200
    assert review_client.calls == []
    buy_signals = [
        signal
        for signal in response.json()["signals"]
        if signal["symbol"] == "600487" and signal["action"] == "buy"
    ]
    assert [signal["timestamp"] for signal in buy_signals] == ["2026-06-05T10:10:00"]
    assert repository.saved_points == []


def test_snapshot_uses_replay_style_context_for_pullback_low_rebound() -> None:
    candles = {
        "600487": _pullback_low_rebound_candles("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(_test_app(minute_provider=FakeProvider(candles), review_client=review_client))

    response = client.get("/api/snapshot")

    payload = response.json()
    buy_signal = [
        signal
        for signal in payload["signals"]
        if signal["symbol"] == "600487" and signal["action"] == "buy"
    ][-1]
    assert response.status_code == 200
    assert "pullback_low_rebound" in buy_signal["rule_ids"]
    assert review_client.calls


def test_snapshot_uses_thirty_minute_replay_context_for_realtime_candidates() -> None:
    candles = {
        "600487": _short_window_sell_spike_candles("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(_test_app(minute_provider=FakeProvider(candles), review_client=review_client))

    response = client.get("/api/snapshot")

    payload = response.json()
    symbol_signals = [signal for signal in payload["signals"] if signal["symbol"] == "600487"]
    assert response.status_code == 200
    assert symbol_signals[-1]["action"] == "hold"
    assert not review_client.calls


def test_snapshot_does_not_review_same_realtime_candidate_twice() -> None:
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(_test_app(minute_provider=FakeProvider(candles), review_client=review_client))

    first = client.get("/api/snapshot")
    second = client.get("/api/snapshot")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(review_client.calls) == 1
    payload = second.json()
    buy_signal = [
        signal
        for signal in payload["signals"]
        if signal["symbol"] == "600487" and signal["kind"] == "candidate_buy"
    ][-1]
    assert buy_signal["llm_status"] == "ok"
    assert buy_signal["llm_review"]["confidence"] == 0.64


def test_realtime_hold_does_not_replace_existing_reviewed_candidate() -> None:
    reviewed_candidate = app_module.Signal(
        symbol="600487",
        timestamp=datetime(2026, 6, 5, 10, 10),
        kind="candidate_buy",
        action="buy",
        confidence=0.64,
        rule_ids=["pullback_low_rebound"],
        reason="低位回抽",
        risks=[],
        source_fresh=True,
        llm_status="ok",
        llm_review={
            "action": "buy",
            "confidence": 0.64,
            "summary": "模型确认可作为低吸观察点",
            "reasons": ["价格低于 VWAP"],
            "risks": [],
            "wait_for": [],
        },
    )
    hold = app_module.Signal(
        symbol="600487",
        timestamp=datetime(2026, 6, 5, 10, 10),
        kind="hold",
        action="hold",
        confidence=0,
        rule_ids=[],
        reason="未满足候选条件",
        risks=[],
        source_fresh=True,
        llm_status="not_requested",
    )
    state = app_module.AppState(
        watchlist=[],
        positions=[],
        candles=[],
        signals=[reviewed_candidate],
        provider_health=app_module.ProviderHealth(provider="test", symbol="600487"),
    )

    app_module._upsert_signal(state, hold)

    assert len(state.signals) == 1
    assert state.signals[0].kind == "candidate_buy"
    assert state.signals[0].llm_status == "ok"


def test_realtime_pending_does_not_replace_existing_reviewed_candidate() -> None:
    reviewed_candidate = app_module.Signal(
        symbol="600487",
        timestamp=datetime(2026, 6, 5, 10, 10),
        kind="candidate_buy",
        action="buy",
        confidence=0.64,
        rule_ids=["pullback_low_rebound"],
        reason="低位回抽",
        risks=[],
        source_fresh=True,
        llm_status="ok",
        llm_review={
            "action": "buy",
            "confidence": 0.64,
            "summary": "模型确认可作为低吸观察点",
            "reasons": ["价格低于 VWAP"],
            "risks": [],
            "wait_for": [],
        },
    )
    pending = app_module.Signal(
        symbol="600487",
        timestamp=datetime(2026, 6, 5, 10, 10),
        kind="candidate_buy",
        action="buy",
        confidence=0.5,
        rule_ids=["pullback_low_rebound"],
        reason="低位回抽",
        risks=[],
        source_fresh=True,
        llm_status="pending",
    )
    state = app_module.AppState(
        watchlist=[],
        positions=[],
        candles=[],
        signals=[reviewed_candidate],
        provider_health=app_module.ProviderHealth(provider="test", symbol="600487"),
    )

    app_module._upsert_signal(state, pending)

    assert len(state.signals) == 1
    assert state.signals[0].llm_status == "ok"
    assert state.signals[0].llm_review is not None


def test_snapshot_keeps_reviewed_candidates_beyond_recent_hold_window() -> None:
    reviewed_candidate = app_module.Signal(
        symbol="600487",
        timestamp=datetime(2026, 6, 5, 10, 10),
        kind="candidate_buy",
        action="buy",
        confidence=0.64,
        rule_ids=["pullback_low_rebound"],
        reason="低位回抽",
        risks=[],
        source_fresh=True,
        llm_status="ok",
        llm_review={
            "action": "buy",
            "confidence": 0.64,
            "summary": "模型确认可作为低吸观察点",
            "reasons": ["价格低于 VWAP"],
            "risks": [],
            "wait_for": [],
        },
    )
    later_holds = [
        app_module.Signal(
            symbol="600487",
            timestamp=datetime(2026, 6, 5, 10, 11 + index),
            kind="hold",
            action="hold",
            confidence=0,
            rule_ids=[],
            reason="未满足候选条件",
            risks=[],
            source_fresh=True,
            llm_status="not_requested",
        )
        for index in range(21)
    ]
    state = app_module.AppState(
        watchlist=[],
        positions=[],
        candles=[
            app_module.Candle(
                symbol="600487",
                timestamp=datetime(2026, 6, 5, 10, 31),
                open=10,
                high=10,
                low=10,
                close=10,
                volume=1000,
            )
        ],
        signals=[reviewed_candidate, *later_holds],
        provider_health=app_module.ProviderHealth(provider="test", symbol="600487"),
    )

    payload = app_module._snapshot(state)

    assert any(
        signal["timestamp"] == "2026-06-05T10:10:00"
        and signal["kind"] == "candidate_buy"
        and signal["llm_status"] == "ok"
        for signal in payload["signals"]
    )


def test_snapshot_merges_realtime_pullback_buy_cluster_like_replay() -> None:
    candles = {
        "600487": [
            _pullback_low_rebound_candles("600487"),
            _pullback_low_rebound_same_cluster_candles("600487"),
        ],
        "300308": [_provider_candles("300308", close=151.6)],
        "300502": [_provider_candles("300502", close=126.8)],
    }
    review_client = FakeReviewClient()
    client = TestClient(_test_app(minute_provider=ProgressiveProvider(candles), review_client=review_client))

    first = client.get("/api/snapshot")
    second = client.get("/api/snapshot")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(review_client.calls) == 1
    buy_signals = [
        signal
        for signal in second.json()["signals"]
        if signal["symbol"] == "600487" and signal["action"] == "buy"
    ]
    assert [signal["timestamp"] for signal in buy_signals] == ["2026-06-05T10:10:00"]


def test_snapshot_reviews_new_lower_realtime_pullback_buy_leg() -> None:
    candles = {
        "600487": [
            _pullback_low_rebound_candles("600487"),
            _pullback_low_rebound_new_low_leg_candles("600487"),
        ],
        "300308": [_provider_candles("300308", close=151.6)],
        "300502": [_provider_candles("300502", close=126.8)],
    }
    review_client = FakeReviewClient()
    client = TestClient(_test_app(minute_provider=ProgressiveProvider(candles), review_client=review_client))

    first = client.get("/api/snapshot")
    second = client.get("/api/snapshot")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(review_client.calls) == 2
    buy_signals = [
        signal
        for signal in second.json()["signals"]
        if signal["symbol"] == "600487" and signal["action"] == "buy"
    ]
    assert [signal["timestamp"] for signal in buy_signals] == [
        "2026-06-05T10:10:00",
        "2026-06-05T10:16:00",
    ]


def test_snapshot_review_context_uses_realtime_candidate_history_without_hold_signals() -> None:
    candles = {
        "600487": [
            _provider_candles("600487", close=23.4),
            _pullback_low_rebound_candles("600487"),
        ],
        "300308": [_provider_candles("300308", close=151.6)],
        "300502": [_provider_candles("300502", close=126.8)],
    }
    review_client = FakeReviewClient()
    client = TestClient(_test_app(minute_provider=ProgressiveProvider(candles), review_client=review_client))

    first = client.get("/api/snapshot")
    second = client.get("/api/snapshot")

    assert first.status_code == 200
    assert second.status_code == 200
    assert _eventually(lambda: len(review_client.calls) == 1)
    assert all(item["kind"] != "hold" for item in review_client.calls[0]["recent_signals"])
    assert review_client.calls[0]["recent_signals"][-1]["timestamp"] == review_client.calls[0]["timestamp"]


def test_today_replay_endpoint_returns_ranked_t_points() -> None:
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _sell_candidate_candles("300308"),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(
        create_app(minute_provider=FakeProvider(candles), review_client=review_client)
    )

    response = client.post("/api/replay/today")

    payload = response.json()
    assert response.status_code == 200
    assert payload["mode"] == "strict"
    assert payload["strict"] is True
    assert payload["date"] == "2026-06-05"
    assert payload["points"]
    assert payload["points"][0]["symbol"] in {"600487", "300308"}
    assert payload["points"][0]["llm_status"] == "ok"
    assert payload["points"][0]["llm_action"] == "buy"
    assert payload["points"][0]["llm_confidence"] == 0.64
    assert payload["points"][0]["llm_reasons"] == ["价格低于 VWAP", "短线量能收缩"]
    assert payload["summary"]["candidate_count"] >= 2


def test_today_replay_endpoint_can_save_json_artifact(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_module, "PROJECT_DIR", tmp_path)
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _sell_candidate_candles("300308"),
        "300502": _provider_candles("300502", close=126.8),
    }
    client = TestClient(
        create_app(minute_provider=FakeProvider(candles), review_client=FakeReviewClient())
    )

    response = client.post("/api/replay/today?save=true")

    payload = response.json()
    assert response.status_code == 200
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.name == "replay-today-strict-2026-06-05.json"
    assert artifact_path.parent == tmp_path / "artifacts"
    assert artifact_path.exists()


def test_today_replay_endpoint_can_return_cached_json_artifact(monkeypatch, tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    artifact_path = artifact_dir / "replay-today-strict-2026-06-05.json"
    artifact_path.write_text(
        (
            '{"date":"2026-06-05","mode":"strict","strict":true,'
            '"points":[{"symbol":"300308"}],"summary":{"candidate_count":1}}'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "PROJECT_DIR", tmp_path)
    client = TestClient(create_app(minute_provider=FakeProvider(MarketDataUnavailable("should not call"))))

    response = client.post("/api/replay/today?cache=true")

    payload = response.json()
    assert response.status_code == 200
    assert payload["points"][0]["symbol"] == "300308"
    assert payload["mode"] == "strict"
    assert Path(payload["artifact_path"]) == artifact_path


def test_today_replay_endpoint_can_run_optimized_analysis_separately() -> None:
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _sell_candidate_candles("300308"),
        "300502": _provider_candles("300502", close=126.8),
    }
    client = TestClient(
        create_app(minute_provider=FakeProvider(candles), review_client=FakeReviewClient())
    )

    response = client.post("/api/replay/today?strict=false")

    payload = response.json()
    assert response.status_code == 200
    assert payload["mode"] == "optimized"
    assert payload["strict"] is False


def test_today_symbol_replay_endpoint_returns_one_symbol_without_model_review() -> None:
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _sell_candidate_candles("300308"),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(create_app(minute_provider=FakeProvider(candles), review_client=review_client))

    response = client.get("/api/replay/today/symbol?symbol=600487")

    payload = response.json()
    assert response.status_code == 200
    assert review_client.calls == []
    assert payload["symbol"] == "600487"
    assert payload["date"] == "2026-06-05"
    assert payload["mode"] == "strict"
    assert payload["strict"] is True
    assert {candle["symbol"] for candle in payload["chart_series"]["realtime"]} == {"600487"}
    assert [point["symbol"] for point in payload["points"]] == ["600487"]
    assert [point["timestamp"] for point in payload["points"]] == sorted(
        point["timestamp"] for point in payload["points"]
    )
    assert payload["points"][0]["llm_status"] == "pending"


def test_today_symbol_replay_endpoint_persists_candidate_points() -> None:
    repository = FakeRepository()
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _sell_candidate_candles("300308"),
        "300502": _provider_candles("300502", close=126.8),
    }
    client = TestClient(
        create_app(
            minute_provider=FakeProvider(candles),
            review_client=FakeReviewClient(),
            repository=repository,
        )
    )

    response = client.get("/api/replay/today/symbol?symbol=600487")

    assert response.status_code == 200
    assert repository.saved_points
    assert repository.saved_points[0].symbol == "600487"
    assert repository.saved_points[0].llm_status == "pending"


def test_today_symbol_review_endpoint_reviews_one_point_with_past_context_only() -> None:
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _sell_candidate_candles("300308"),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(create_app(minute_provider=FakeProvider(candles), review_client=review_client))
    replay_payload = client.get("/api/replay/today/symbol?symbol=600487").json()
    timestamp = replay_payload["points"][0]["timestamp"]

    response = client.post(
        "/api/replay/today/review",
        params={"symbol": "600487", "timestamp": timestamp},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["symbol"] == "600487"
    assert payload["timestamp"] == timestamp
    assert payload["llm_status"] == "ok"
    assert payload["llm_action"] == "buy"
    assert len(review_client.calls) == 1
    assert all(candle["timestamp"] <= timestamp for candle in review_client.calls[0]["one_minute_candles"])


def test_today_symbol_review_endpoint_persists_reviewed_point() -> None:
    repository = FakeRepository()
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _sell_candidate_candles("300308"),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(
        create_app(minute_provider=FakeProvider(candles), review_client=review_client, repository=repository)
    )
    timestamp = client.get("/api/replay/today/symbol?symbol=600487").json()["points"][0]["timestamp"]

    response = client.post("/api/replay/today/review", params={"symbol": "600487", "timestamp": timestamp})

    assert response.status_code == 200
    assert repository.saved_points[-1].llm_status == "ok"
    assert repository.saved_points[-1].llm_action == "buy"


def test_trading_days_endpoint_fetches_and_caches_when_database_is_empty() -> None:
    repository = FakeRepository()
    provider = FakeProvider({"300308": _multi_day_candidate_candles("300308")})
    client = TestClient(create_app(minute_provider=provider, repository=repository))

    response = client.get("/api/trading-days?symbol=300308")

    payload = response.json()
    assert response.status_code == 200
    assert repository.schema_initialized is True
    assert provider.calls == ["300308"]
    assert payload == {
        "symbol": "300308",
        "days": ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"],
    }
    assert repository.saved_bars


def test_day_endpoint_returns_database_bars_chart_series_and_stored_points_without_generating() -> None:
    repository = FakeRepository()
    trade_date = date(2026, 6, 5)
    repository.save_minute_bars(_session_provider_candles("300308", close=151.6), source="test")
    repository.save_quote(_quote("300308", "中际旭创", latest=151.6, previous_close=150, open_price=150.8), trade_date, "test")
    repository.save_replay_points(
        [
            ReplayPoint(
                symbol="300308",
                timestamp="2026-06-05T10:05:00",
                action="buy",
                kind="candidate_buy",
                price=150.2,
                confidence=0.72,
                rule_ids=["vwap_deviation"],
                reason="历史低吸候选",
                risks=["趋势仍弱"],
                llm_status="ok",
                llm_action="buy",
                llm_confidence=0.66,
                llm_summary="历史复核点位，日期行情接口应只读返回",
            )
        ],
        strict=True,
    )
    repository.saved_points.clear()
    provider = FakeProvider(MarketDataUnavailable("should not call"))
    client = TestClient(create_app(minute_provider=provider, repository=repository))

    response = client.get("/api/day?symbol=300308&date=2026-06-05")

    payload = response.json()
    assert response.status_code == 200
    assert provider.calls == []
    assert payload["symbol"] == "300308"
    assert payload["date"] == "2026-06-05"
    assert set(payload["chart_series"]) == {"realtime", "one_minute", "five_minute"}
    assert len(payload["chart_series"]["one_minute"]) == 11
    assert len(payload["points"]) == 1
    assert payload["points"][0]["timestamp"] == "2026-06-05T10:05:00"
    assert payload["points"][0]["action"] == "buy"
    assert payload["points"][0]["llm_summary"] == "历史复核点位，日期行情接口应只读返回"
    assert repository.saved_points == []
    assert payload["quote"]["symbol"] == "300308"


def test_day_endpoint_refetches_invalid_cached_zero_close_bars() -> None:
    repository = FakeRepository()
    trade_date = date(2026, 6, 9)
    repository.save_minute_bars(
        [
            Candle(
                symbol="300308",
                timestamp=datetime(2026, 6, 9, 9, 31),
                open=1140.97,
                high=1140.97,
                low=1140.97,
                close=0,
                volume=7940,
            )
        ],
        source="bad-cache",
    )
    provider = RepairingProvider(_session_provider_candles_on("300308", trade_date, close=1184.99))
    client = TestClient(create_app(minute_provider=provider, repository=repository))

    response = client.get("/api/day?symbol=300308&date=2026-06-09")

    payload = response.json()
    assert response.status_code == 200
    assert provider.fetch_for_date_calls == [("300308", trade_date)]
    assert len(payload["chart_series"]["one_minute"]) == 11
    assert payload["chart_series"]["one_minute"][0]["close"] > 0


def test_day_endpoint_fetches_requested_uncached_date_and_persists_bars() -> None:
    repository = FakeRepository()
    provider = FakeProvider(
        {
            "300308": [
                Candle(
                    symbol="300308",
                    timestamp="2026-05-28T09:30:00",
                    open=150.0,
                    high=150.5,
                    low=149.8,
                    close=150.2,
                    volume=1200,
                )
            ]
        }
    )
    client = TestClient(create_app(minute_provider=provider, repository=repository))

    response = client.get("/api/day?symbol=300308&date=2026-05-28")

    payload = response.json()
    assert response.status_code == 200
    assert provider.day_calls == [("300308", date(2026, 5, 28))]
    assert payload["date"] == "2026-05-28"
    assert payload["chart_series"]["one_minute"][0]["timestamp"] == "2026-05-28T09:30:00"
    assert repository.get_minute_bars("300308", date(2026, 5, 28))
    assert repository.list_trading_days("300308") == ["2026-05-28"]


def test_day_endpoint_returns_404_when_requested_date_has_no_bars() -> None:
    repository = FakeRepository()
    provider = FakeProvider(MarketDataUnavailable("No minute data returned for 300308 on 2026-05-28"))
    client = TestClient(create_app(minute_provider=provider, repository=repository))

    response = client.get("/api/day?symbol=300308&date=2026-05-28")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "minute_bars_not_found"
    assert "2026-05-28" in response.json()["detail"]["message"]


def test_day_endpoint_returns_503_when_requested_date_market_channel_fails() -> None:
    repository = FakeRepository()
    provider = FakeProvider(MarketDataChannelUnavailable("Eastmoney minute data channel unavailable: disconnected"))
    client = TestClient(create_app(minute_provider=provider, repository=repository))

    response = client.get("/api/day?symbol=300308&date=2026-05-28")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "market_data_channel_unavailable"
    assert detail["provider"] == "market_provider"
    assert "行情源暂不可用" in detail["message"]
    assert "disconnected" in detail["reason"]


def test_day_replay_endpoint_runs_strict_replay_for_date_and_persists_points() -> None:
    repository = FakeRepository()
    repository.save_minute_bars(_buy_candidate_candles("600487"), source="test")
    provider = FakeProvider(MarketDataUnavailable("should not call"))
    client = TestClient(create_app(minute_provider=provider, repository=repository))

    response = client.post("/api/day/replay?symbol=600487&date=2026-06-05&strict=true&review=false")

    payload = response.json()
    assert response.status_code == 200
    assert payload["symbol"] == "600487"
    assert payload["date"] == "2026-06-05"
    assert payload["points"]
    assert repository.saved_points
    assert repository.get_replay_points("600487", date(2026, 6, 5), True)


def test_day_replay_endpoint_does_not_mark_restore_until_trade_is_confirmed() -> None:
    repository = FakeRepository()
    repository.save_minute_bars(_sell_then_buy_candles("300308"), source="test")
    provider = FakeProvider(MarketDataUnavailable("should not call"))
    client = TestClient(create_app(minute_provider=provider, repository=repository))

    response = client.post("/api/day/replay?symbol=300308&date=2026-06-05&strict=true&review=false")

    payload = response.json()
    assert response.status_code == 200
    assert payload["points"][0]["action"] == "sell"
    assert any(point["action"] == "buy" for point in payload["points"])
    assert all("restore_after_intraday_sell" not in point["rule_ids"] for point in payload["points"])
    assert repository.saved_points


def test_day_replay_review_endpoint_reviews_requested_date_symbol_point() -> None:
    repository = FakeRepository()
    repository.save_minute_bars(_buy_candidate_candles_on("300502", date(2026, 6, 1)), source="test")
    repository.save_minute_bars(_sell_candidate_candles("600487"), source="test")
    review_client = FakeReviewClient()
    provider = FakeProvider(MarketDataUnavailable("should not call"))
    client = TestClient(
        create_app(minute_provider=provider, review_client=review_client, repository=repository)
    )
    replay_payload = client.post(
        "/api/day/replay?symbol=300502&date=2026-06-01&strict=true&review=false"
    ).json()
    timestamp = replay_payload["points"][0]["timestamp"]

    response = client.post(
        "/api/day/replay/review",
        params={"symbol": "300502", "date": "2026-06-01", "timestamp": timestamp, "strict": True},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["symbol"] == "300502"
    assert payload["timestamp"] == timestamp
    assert payload["llm_status"] == "ok"
    assert payload["llm_action"] == "buy"
    assert len(review_client.calls) == 1
    assert review_client.calls[0]["symbol"] == "300502"
    assert all(candle["timestamp"] <= timestamp for candle in review_client.calls[0]["one_minute_candles"])
    assert repository.saved_points[-1].symbol == "300502"
    assert datetime.fromisoformat(repository.saved_points[-1].timestamp).date() == date(2026, 6, 1)


def test_day_replay_review_endpoint_sends_feishu_when_review_day_notifications_enabled() -> None:
    repository = FakeRepository()
    repository.set_bool_setting("review_day_feishu_enabled", True)
    repository.save_minute_bars(_buy_candidate_candles_on("300502", date(2026, 6, 1)), source="test")
    repository.save_minute_bars(_sell_candidate_candles("600487"), source="test")
    notifier = FakeMonitorNotifier()
    client = TestClient(
        create_app(
            minute_provider=FakeProvider(MarketDataUnavailable("should not call")),
            review_client=FakeReviewClient(),
            repository=repository,
            monitor_notifier=notifier,
        )
    )
    replay_payload = client.post(
        "/api/day/replay?symbol=300502&date=2026-06-01&strict=true&review=false"
    ).json()

    response = client.post(
        "/api/day/replay/review",
        params={
            "symbol": "300502",
            "date": "2026-06-01",
            "timestamp": replay_payload["points"][0]["timestamp"],
            "strict": True,
        },
    )

    assert response.status_code == 200
    assert len(notifier.messages) == 1
    assert "【T Maker 复核日通知】" in notifier.messages[0]
    assert "300502" in notifier.messages[0]
    assert "工程 AI：低吸" in notifier.messages[0]


def test_recent_replay_endpoint_returns_grouped_days_and_accuracy_summary(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_module, "PROJECT_DIR", tmp_path)
    candles = {
        "300308": _multi_day_candidate_candles("300308"),
        "300502": _multi_day_candidate_candles("300502"),
        "600487": _multi_day_candidate_candles("600487"),
    }
    client = TestClient(
        create_app(minute_provider=FakeProvider(candles), review_client=FakeReviewClient())
    )

    response = client.post("/api/replay/recent?days=5&save=true&review=true")

    payload = response.json()
    assert response.status_code == 200
    assert payload["days_requested"] == 5
    assert payload["symbols"] == ["300308", "300502", "600487", "000636"]
    assert [day["date"] for day in payload["days"]] == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
        "2026-06-05",
    ]
    assert payload["summary"]["trading_day_count"] == 5
    assert payload["summary"]["reviewed_count"] >= 5
    assert payload["summary"]["ai_buy_count"] >= 1
    assert payload["summary"]["accuracy_checked_count"] >= 1
    assert "accuracy_rate_pct" in payload["summary"]
    assert payload["mode"] == "strict"
    assert payload["strict"] is True
    assert payload["schema_version"] == 2
    assert set(payload["days"][0]["chart_series"]) == {"realtime", "one_minute", "five_minute"}
    assert payload["days"][0]["chart_series"]["one_minute"][0]["timestamp"].startswith("2026-06-01T")
    assert payload["days"][0]["chart_series"]["five_minute"]
    assert payload["artifact_path"].endswith("replay-recent-5d-strict-reviewed-2026-06-01_2026-06-05.json")


def test_recent_replay_endpoint_can_skip_model_review_for_fast_accuracy_check() -> None:
    candles = {
        "300308": _multi_day_candidate_candles("300308"),
        "300502": _multi_day_candidate_candles("300502"),
        "600487": _multi_day_candidate_candles("600487"),
    }
    review_client = FakeReviewClient()
    client = TestClient(create_app(minute_provider=FakeProvider(candles), review_client=review_client))

    response = client.post("/api/replay/recent?days=5&review=false")

    payload = response.json()
    assert response.status_code == 200
    assert payload["mode"] == "strict"
    assert payload["strict"] is True
    assert review_client.calls == []
    assert payload["summary"]["reviewed_count"] == 0
    assert payload["summary"]["accuracy_checked_count"] >= 1


def test_recent_replay_cache_is_separated_by_review_mode(monkeypatch, tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    fast_path = artifact_dir / "replay-recent-5d-strict-fast-2026-06-01_2026-06-05.json"
    reviewed_path = artifact_dir / "replay-recent-5d-strict-reviewed-2026-06-01_2026-06-05.json"
    fast_path.write_text(
        '{"schema_version":2,"mode":"strict","strict":true,"review_enabled":false,'
        '"days_requested":5,"symbols":[],"days":[],"summary":{"reviewed_count":0}}',
        encoding="utf-8",
    )
    reviewed_path.write_text(
        '{"schema_version":2,"mode":"strict","strict":true,"review_enabled":true,'
        '"days_requested":5,"symbols":[],"days":[],"summary":{"reviewed_count":3}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "PROJECT_DIR", tmp_path)
    client = TestClient(create_app(minute_provider=FakeProvider(MarketDataUnavailable("should not call"))))

    fast_response = client.post("/api/replay/recent?days=5&cache=true&review=false")
    reviewed_response = client.post("/api/replay/recent?days=5&cache=true&review=true")

    assert fast_response.status_code == 200
    assert reviewed_response.status_code == 200
    assert fast_response.json()["review_enabled"] is False
    assert reviewed_response.json()["review_enabled"] is True
    assert fast_response.json()["summary"]["reviewed_count"] == 0
    assert reviewed_response.json()["summary"]["reviewed_count"] == 3


def _buy_candidate_candles(symbol: str) -> list[Candle]:
    closes = [10.5, 10.2, 9.9, 9.6, 9.55]
    volumes = [500, 420, 340, 260, 180]
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T10:{index + 1:02d}:00",
            open=close + 0.05,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def _buy_candidate_candles_on(symbol: str, trade_date: date) -> list[Candle]:
    closes = [10.5, 10.2, 9.9, 9.6, 9.55]
    volumes = [500, 420, 340, 260, 180]
    start = datetime.combine(trade_date, datetime.strptime("10:01", "%H:%M").time())
    return [
        Candle(
            symbol=symbol,
            timestamp=start.replace(minute=index + 1),
            open=close + 0.05,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def _flat_hold_candles_after_candidate_time(symbol: str) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T10:{index + 1:02d}:00",
            open=10,
            high=10.05,
            low=9.95,
            close=10,
            volume=1000,
        )
        for index in range(6)
    ]


def _sell_candidate_candles(symbol: str) -> list[Candle]:
    closes = [10.0, 10.1, 10.2, 10.8, 11.0]
    volumes = [100, 110, 120, 180, 220]
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T10:{index + 1:02d}:00",
            open=close - 0.03,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def _sell_then_buy_candles(symbol: str) -> list[Candle]:
    closes = [10.0, 10.1, 10.2, 10.8, 11.0, 10.7, 10.2, 9.7, 9.2]
    volumes = [100, 110, 120, 180, 220, 300, 260, 220, 180]
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T10:{index + 1:02d}:00",
            open=close,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def _pullback_low_rebound_candles(symbol: str) -> list[Candle]:
    closes = [100.0, 101.8, 103.4, 104.5, 104.3, 103.7, 103.0, 102.5, 102.0, 102.25]
    volumes = [500, 700, 1000, 1800, 1600, 1300, 1100, 900, 1900, 900]
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T10:{index + 1:02d}:00",
            open=close,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def _pullback_low_rebound_same_cluster_candles(symbol: str) -> list[Candle]:
    closes = [102.1, 101.95, 101.85, 101.72, 101.88]
    volumes = [850, 830, 810, 1200, 700]
    return [
        *_pullback_low_rebound_candles(symbol),
        *[
            Candle(
                symbol=symbol,
                timestamp=f"2026-06-05T10:{11 + index:02d}:00",
                open=close,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=volumes[index],
            )
            for index, close in enumerate(closes)
        ],
    ]


def _pullback_low_rebound_new_low_leg_candles(symbol: str) -> list[Candle]:
    closes = [102.0, 101.7, 101.35, 101.1, 101.3, 101.6]
    volumes = [850, 820, 780, 1300, 900, 700]
    return [
        *_pullback_low_rebound_candles(symbol),
        *[
            Candle(
                symbol=symbol,
                timestamp=f"2026-06-05T10:{11 + index:02d}:00",
                open=close,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=volumes[index],
            )
            for index, close in enumerate(closes)
        ],
    ]


def _short_window_sell_spike_candles(symbol: str) -> list[Candle]:
    closes = [93.0] * 22 + [90.0] * 7 + [93.5]
    return [
        Candle(
            symbol=symbol,
            timestamp=f"2026-06-05T10:{index + 1:02d}:00",
            open=close,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=1000,
        )
        for index, close in enumerate(closes)
    ]


def _multi_day_candidate_candles(symbol: str) -> list[Candle]:
    candles: list[Candle] = []
    for day in range(5):
        trade_date = datetime(2026, 6, 1 + day, 10, 1)
        closes = [10.5, 10.2, 9.9, 9.6, 9.55, 9.7, 9.85, 10.0, 10.1]
        volumes = [500, 420, 340, 260, 180, 220, 260, 300, 330]
        for index, close in enumerate(closes):
            candles.append(
                Candle(
                    symbol=symbol,
                    timestamp=trade_date.replace(minute=index + 1),
                    open=close + 0.05,
                    high=close + 0.1,
                    low=close - 0.1,
                    close=close,
                    volume=volumes[index],
                )
            )
    return candles
