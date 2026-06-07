from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

import tmaker.api.app as app_module
from tmaker.api.app import create_app
from tmaker.domain.models import Candle, MarketQuote
from tmaker.market.akshare_provider import MarketDataChannelUnavailable, MarketDataUnavailable
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
            return self.candles[symbol]
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


class FakeRepository:
    def __init__(self) -> None:
        self.minute_bars: dict[tuple[str, date], list[Candle]] = {}
        self.points: dict[tuple[str, date, bool], list[ReplayPoint]] = {}
        self.quotes: dict[tuple[str, date], MarketQuote] = {}
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


def test_health_endpoint_reports_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_snapshot_endpoint_returns_watchlist_positions_and_signals() -> None:
    client = TestClient(create_app(minute_provider=FakeProvider(_provider_candles())))

    response = client.get("/api/snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert [(item["symbol"], item["name"]) for item in payload["watchlist"]] == [
        ("300308", "中际旭创"),
        ("300502", "新易盛"),
        ("600487", "亨通光电"),
    ]
    assert {position["symbol"] for position in payload["positions"]} == {"300308", "300502", "600487"}
    positions_by_symbol = {position["symbol"]: position for position in payload["positions"]}
    assert positions_by_symbol["300308"]["base_quantity"] == 200
    assert positions_by_symbol["300502"]["base_quantity"] == 200
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
        "600487": _provider_candles("600487", close=23.4),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    provider = FakeProvider(candles)
    client = TestClient(create_app(minute_provider=provider))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert provider.calls == ["300308", "300502", "600487"]
    assert payload["provider_health"]["provider"] == "tencent_ifzq"
    assert payload["provider_health"]["last_success_at"] == "2026-06-05T09:32:00"
    assert {candle["symbol"] for candle in payload["candles"]} == {"300308", "300502", "600487"}
    assert payload["candles"][-1]["close"] == 23.4


def test_snapshot_returns_realtime_quotes_when_available() -> None:
    candles = {
        "600487": _provider_candles("600487", close=23.4),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    quotes = {
        "600487": _quote("600487", "亨通光电", latest=23.8, previous_close=23.2, open_price=23.3),
        "300308": _quote("300308", "中际旭创", latest=1179.99, previous_close=1280, open_price=1273.2),
        "300502": _quote("300502", "新易盛", latest=748, previous_close=775.94, open_price=790),
    }
    provider = FakeProvider(candles, quotes=quotes)
    client = TestClient(create_app(minute_provider=provider))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert provider.quote_calls == ["300308", "300502", "600487"]
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
        "600487": _many_provider_candles("600487", close=23.4),
        "300308": _many_provider_candles("300308", close=151.6),
        "300502": _many_provider_candles("300502", close=126.8),
    }
    client = TestClient(create_app(minute_provider=FakeProvider(candles)))

    response = client.get("/api/snapshot")

    payload = response.json()
    candles_by_symbol = {
        symbol: [candle for candle in payload["candles"] if candle["symbol"] == symbol]
        for symbol in ["300308", "300502", "600487"]
    }
    assert {symbol: len(candles) for symbol, candles in candles_by_symbol.items()} == {
        "300308": 100,
        "300502": 100,
        "600487": 100,
    }
    assert candles_by_symbol["600487"][-1]["close"] == 23.4
    assert candles_by_symbol["300308"][-1]["close"] == 151.6
    assert candles_by_symbol["300502"][-1]["close"] == 126.8


def test_snapshot_returns_realtime_one_minute_and_five_minute_series() -> None:
    candles = {
        "600487": _session_provider_candles("600487", close=23.4),
        "300308": _session_provider_candles("300308", close=151.6),
        "300502": _session_provider_candles("300502", close=126.8),
    }
    client = TestClient(create_app(minute_provider=FakeProvider(candles)))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert set(payload["chart_series"]) == {"realtime", "one_minute", "five_minute"}
    assert {candle["symbol"] for candle in payload["chart_series"]["realtime"]} == {
        "300308",
        "300502",
        "600487",
    }
    assert {candle["symbol"] for candle in payload["chart_series"]["one_minute"]} == {
        "300308",
        "300502",
        "600487",
    }
    five_minute_by_symbol = {
        symbol: [candle for candle in payload["chart_series"]["five_minute"] if candle["symbol"] == symbol]
        for symbol in ["300308", "300502", "600487"]
    }
    assert {symbol: len(candles) for symbol, candles in five_minute_by_symbol.items()} == {
        "300308": 2,
        "300502": 2,
        "600487": 2,
    }
    assert five_minute_by_symbol["600487"][-1]["timestamp"] == "2026-06-05T09:40:00"


def test_snapshot_realtime_series_keeps_full_trading_day() -> None:
    candles = {
        "600487": _full_trading_day_provider_candles("600487", close=23.4),
        "300308": _full_trading_day_provider_candles("300308", close=151.6),
        "300502": _full_trading_day_provider_candles("300502", close=126.8),
    }
    client = TestClient(create_app(minute_provider=FakeProvider(candles)))

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


def test_snapshot_falls_back_to_demo_data_when_provider_fails() -> None:
    provider = FakeProvider(MarketDataUnavailable("empty"))
    client = TestClient(create_app(minute_provider=provider))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert response.status_code == 200
    assert payload["provider_health"]["provider"] == "tencent_ifzq_fallback"
    assert payload["provider_health"]["missing_candle_count"] >= 1
    assert payload["provider_health"]["last_error"] == (
        "300308: empty；300502: empty；600487: empty"
    )
    assert payload["candles"]


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
    client = TestClient(create_app(minute_provider=provider))

    response = client.get("/api/snapshot")

    payload = response.json()
    assert response.status_code == 200
    assert payload["provider_health"]["provider"] == "tencent_ifzq_fallback"
    assert payload["provider_health"]["last_error"] == (
        "300308: proxy unavailable；300502: proxy unavailable；600487: proxy unavailable"
    )


def test_snapshot_reviews_candidate_signals_with_llm_client() -> None:
    candles = {
        "600487": _buy_candidate_candles("600487"),
        "300308": _provider_candles("300308", close=151.6),
        "300502": _provider_candles("300502", close=126.8),
    }
    review_client = FakeReviewClient()
    client = TestClient(
        create_app(minute_provider=FakeProvider(candles), review_client=review_client)
    )

    response = client.get("/api/snapshot")

    payload = response.json()
    buy_signal = [
        signal
        for signal in payload["signals"]
        if signal["symbol"] == "600487" and signal["kind"] == "candidate_buy"
    ][-1]
    assert review_client.calls
    assert review_client.calls[0]["symbol"] == "600487"
    assert buy_signal["llm_status"] == "ok"
    assert buy_signal["llm_review"]["summary"] == "模型确认可作为低吸观察点"


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
    assert payload["symbols"] == ["300308", "300502", "600487"]
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
