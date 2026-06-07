from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel, Field

from tmaker.config import PROJECT_DIR, get_settings
from tmaker.domain.models import Candle, MarketQuote, Position, ProviderHealth, Signal
from tmaker.llm.context import build_review_context
from tmaker.llm.openai_client import OpenAICompatibleClient
from tmaker.llm.review import LlmReviewer, ReviewClient
from tmaker.market.akshare_provider import MarketDataChannelUnavailable, MarketDataUnavailable
from tmaker.market.eastmoney_provider import EastmoneyHistoricalMinuteProvider
from tmaker.market.bars import aggregate_five_minute
from tmaker.market.tencent_provider import TencentHistoricalMinuteProvider, TencentMarketProvider
from tmaker.storage.postgres import PostgresRepository
from tmaker.strategy.market_context import build_equal_weight_sector_candles, build_market_context
from tmaker.strategy.replay import replay_recent_days, replay_symbol_today, replay_today, review_symbol_point
from tmaker.strategy.rules import evaluate_signal


class WatchSymbol(BaseModel):
    symbol: str
    name: str
    status: str = "watching"


class AppState(BaseModel):
    watchlist: list[WatchSymbol]
    positions: list[Position]
    candles: list[Candle]
    quotes: dict[str, MarketQuote] = Field(default_factory=dict)
    signals: list[Signal]
    provider_health: ProviderHealth


def create_app(
    minute_provider: TencentMarketProvider | None = None,
    review_client: ReviewClient | None = None,
    repository: PostgresRepository | None = None,
) -> FastAPI:
    app = FastAPI(title="T Maker API")
    state = _initial_state()
    settings = get_settings()
    provider = minute_provider or TencentMarketProvider()
    multi_day_provider = minute_provider or TencentHistoricalMinuteProvider()
    day_provider = minute_provider or EastmoneyHistoricalMinuteProvider()
    reviewer = LlmReviewer(review_client or _default_review_client())
    repo = repository or PostgresRepository(settings.database_url)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/snapshot")
    def snapshot() -> dict:
        _refresh_from_provider(state, provider, reviewer)
        return _snapshot(state)

    @app.get("/api/trading-days")
    def trading_days(symbol: str) -> dict:
        _ensure_watch_symbol(state, symbol)
        repo.init_schema()
        days = repo.list_trading_days(symbol)
        if not days:
            _fetch_and_cache_symbol_history(repo, multi_day_provider, symbol)
            days = repo.list_trading_days(symbol)
        return {"symbol": symbol, "days": days}

    @app.get("/api/day")
    def day_snapshot(symbol: str, date: str) -> dict:
        _ensure_watch_symbol(state, symbol)
        trade_date = _parse_trade_date(date)
        repo.init_schema()
        candles = _get_or_fetch_day_candles(repo, day_provider, symbol, trade_date)
        quote = repo.get_quote(symbol, trade_date)
        points = repo.get_replay_points(symbol, trade_date, strict=True)
        return _day_payload(symbol, trade_date, candles, points, quote)

    @app.post("/api/day/replay")
    def day_replay(symbol: str, date: str, strict: bool = True, review: bool = False) -> dict:
        _ensure_watch_symbol(state, symbol)
        trade_date = _parse_trade_date(date)
        repo.init_schema()
        candles = _get_or_fetch_day_candles(repo, day_provider, symbol, trade_date)
        day_candles_by_symbol = _context_day_candles(repo, day_provider, state, symbol, trade_date, candles)
        static_day_provider = _StaticDayProvider(day_candles_by_symbol)
        if review:
            result = replay_today(static_day_provider, [symbol], state.positions, reviewer.client, strict=strict)
            points = result.points
            summary = result.summary
            mode = result.mode
        else:
            result = replay_symbol_today(
                static_day_provider,
                symbol,
                state.positions,
                strict=strict,
                context_symbols=list(day_candles_by_symbol),
            )
            points = result.points
            summary = result.summary
            mode = result.mode
        repo.replace_replay_points_for_day(symbol, trade_date, points, strict=strict)
        quote = repo.get_quote(symbol, trade_date)
        return {
            **_day_payload(symbol, trade_date, candles, points, quote),
            "mode": mode,
            "strict": strict,
            "summary": summary,
        }

    @app.post("/api/day/replay/review")
    def day_replay_review(symbol: str, date: str, timestamp: str, strict: bool = True) -> dict:
        _ensure_watch_symbol(state, symbol)
        trade_date = _parse_trade_date(date)
        repo.init_schema()
        candles = _get_or_fetch_day_candles(repo, day_provider, symbol, trade_date)
        day_candles_by_symbol = _context_day_candles(repo, day_provider, state, symbol, trade_date, candles)
        static_day_provider = _StaticDayProvider(day_candles_by_symbol)
        point = review_symbol_point(
            static_day_provider,
            symbol,
            timestamp,
            state.positions,
            reviewer.client,
            strict=strict,
            context_symbols=list(day_candles_by_symbol),
        )
        if point is None:
            raise HTTPException(status_code=404, detail="Replay point not found")
        repo.save_replay_points([point], strict=strict)
        return point.model_dump(mode="json")

    @app.post("/api/simulate/tick")
    def simulate_tick() -> dict:
        next_candle = _next_demo_candle(state.candles)
        state.candles.append(next_candle)
        state.provider_health = state.provider_health.model_copy(
            update={"last_success_at": next_candle.timestamp, "latency_ms": 180}
        )
        position = _position_for_symbol(state.positions, next_candle.symbol)
        signal = evaluate_signal(
            state.candles[-8:],
            [],
            position,
            state.provider_health,
            now=next_candle.timestamp,
        )
        signal = _review_candidate_signal(signal, state.candles[-30:], position, state, reviewer)
        state.signals.append(signal)
        return _snapshot(state)

    @app.post("/api/replay/today")
    def replay_today_endpoint(save: bool = False, cache: bool = False, strict: bool = True) -> dict:
        if cache:
            cached = _load_saved_replay_result(strict)
            if cached is not None:
                return cached

        result = replay_today(
            provider,
            [item.symbol for item in state.watchlist],
            state.positions,
            reviewer.client,
            strict=strict,
        )
        payload = result.model_dump(mode="json")
        if save:
            payload["artifact_path"] = str(_save_replay_result(payload, result.date, strict))
        return payload

    @app.get("/api/replay/today/symbol")
    def replay_today_symbol_endpoint(symbol: str, strict: bool = True) -> dict:
        _ensure_watch_symbol(state, symbol)
        result = replay_symbol_today(provider, symbol, state.positions, strict=strict)
        repo.init_schema()
        trade_date = date_type.fromisoformat(result.date) if result.date else date_type.today()
        repo.replace_replay_points_for_day(symbol, trade_date, result.points, strict=strict)
        return {
            "symbol": symbol,
            **result.model_dump(mode="json"),
        }

    @app.post("/api/replay/today/review")
    def replay_today_review_endpoint(symbol: str, timestamp: str, strict: bool = True) -> dict:
        _ensure_watch_symbol(state, symbol)
        point = review_symbol_point(
            provider,
            symbol,
            timestamp,
            state.positions,
            reviewer.client,
            strict=strict,
        )
        if point is None:
            raise HTTPException(status_code=404, detail="Replay point not found")
        repo.init_schema()
        repo.save_replay_points([point], strict=strict)
        return point.model_dump(mode="json")

    @app.post("/api/replay/recent")
    def replay_recent_endpoint(
        days: int = 5,
        save: bool = False,
        cache: bool = False,
        review: bool = False,
        strict: bool = True,
    ) -> dict:
        bounded_days = min(max(days, 1), 10)
        if cache:
            cached = _load_saved_recent_replay_result(bounded_days, strict, review)
            if cached is not None:
                return cached

        result = replay_recent_days(
            multi_day_provider,
            [item.symbol for item in state.watchlist],
            state.positions,
            reviewer.client if review else None,
            days=bounded_days,
            strict=strict,
        )
        payload = result.model_dump(mode="json")
        if save:
            payload["artifact_path"] = str(_save_recent_replay_result(payload, bounded_days, strict, review))
        return payload

    return app


def _refresh_from_provider(
    state: AppState,
    provider: TencentMarketProvider,
    reviewer: LlmReviewer,
) -> None:
    all_candles: list[Candle] = []
    candles_by_symbol: dict[str, list[Candle]] = {}
    errors: list[str] = []
    latest: Candle | None = None

    for item in state.watchlist:
        try:
            candles = provider.fetch_minutes(item.symbol)
        except Exception as exc:
            errors.append(f"{item.symbol}: {_format_provider_error(exc)}")
            continue

        if not candles:
            errors.append(f"{item.symbol}: Tencent minute provider returned no candles")
            continue

        all_candles.extend(candles)
        candles_by_symbol[item.symbol] = candles
        try:
            state.quotes[item.symbol] = provider.fetch_quote(item.symbol)
        except Exception as exc:
            state.quotes.pop(item.symbol, None)
            errors.append(f"{item.symbol} quote: {_format_provider_error(exc)}")
        current_latest = candles[-1]
        latest = current_latest if latest is None or current_latest.timestamp >= latest.timestamp else latest

    if not all_candles or latest is None:
        state.provider_health = state.provider_health.model_copy(
            update={
                "provider": "tencent_ifzq_fallback",
                "missing_candle_count": state.provider_health.missing_candle_count + 1,
                "last_error": "；".join(errors) if errors else "Tencent minute provider returned no candles",
            }
        )
        return

    state.candles = all_candles
    for item in state.watchlist:
        candles = candles_by_symbol.get(item.symbol, [])
        if not candles:
            continue
        current_latest = candles[-1]
        position = _position_for_symbol(state.positions, item.symbol)
        health = ProviderHealth(
            provider="tencent_ifzq",
            symbol=item.symbol,
            last_success_at=current_latest.timestamp,
            latency_ms=0,
            missing_candle_count=0,
            last_error=None,
        )
        market_context = _market_context_for_symbol(item.symbol, candles, candles_by_symbol)
        signal = evaluate_signal(
            candles[-8:],
            [],
            position,
            health,
            now=current_latest.timestamp,
            session_candles=candles,
            market_context=market_context,
        )
        signal = _review_candidate_signal(
            signal,
            candles[-30:],
            position,
            state,
            reviewer,
            market_context=market_context.model_dump(mode="json") if market_context else None,
        )
        _upsert_signal(state, signal)

    state.provider_health = ProviderHealth(
        provider="tencent_ifzq",
        symbol=",".join(item.symbol for item in state.watchlist),
        last_success_at=latest.timestamp,
        latency_ms=0,
        missing_candle_count=len(errors),
        last_error="；".join(errors) if errors else None,
    )


def _initial_state() -> AppState:
    start = datetime(2026, 6, 5, 10, 1)
    closes = [10.5, 10.2, 9.9, 9.6, 9.55]
    volumes = [500, 420, 340, 260, 180]
    candles = [
        Candle(
            symbol="300308",
            timestamp=start + timedelta(minutes=index),
            open=close + 0.05,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]
    health = ProviderHealth(
        provider="tencent_ifzq",
        symbol="300308",
        last_success_at=candles[-1].timestamp,
        latency_ms=180,
    )
    positions = [
        Position(
            symbol="300308",
            base_quantity=200,
            cost_price=0,
            available_cash=200000,
            t_quantity=100,
        ),
        Position(
            symbol="300502",
            base_quantity=200,
            cost_price=0,
            available_cash=200000,
            t_quantity=100,
        ),
        Position(
            symbol="600487",
            base_quantity=0,
            cost_price=0,
            available_cash=200000,
            t_quantity=100,
        ),
    ]
    initial_signal = evaluate_signal(candles, [], positions[0], health, now=candles[-1].timestamp)
    return AppState(
        watchlist=[
            WatchSymbol(symbol="300308", name="中际旭创"),
            WatchSymbol(symbol="300502", name="新易盛"),
            WatchSymbol(symbol="600487", name="亨通光电"),
        ],
        positions=positions,
        candles=candles,
        quotes={},
        signals=[initial_signal],
        provider_health=health,
    )


def _snapshot(state: AppState) -> dict:
    realtime = _trading_day_candles_by_symbol(state)
    one_minute = realtime
    five_minute = _five_minute_candles_by_symbol(state)
    return {
        "watchlist": [item.model_dump(mode="json") for item in state.watchlist],
        "positions": [position.model_dump(mode="json") for position in state.positions],
        "quotes": {symbol: quote.model_dump(mode="json") for symbol, quote in state.quotes.items()},
        "candles": [candle.model_dump(mode="json") for candle in one_minute],
        "chart_series": {
            "realtime": [candle.model_dump(mode="json") for candle in realtime],
            "one_minute": [candle.model_dump(mode="json") for candle in one_minute],
            "five_minute": [candle.model_dump(mode="json") for candle in five_minute],
        },
        "signals": [signal.model_dump(mode="json") for signal in state.signals[-20:]],
        "provider_health": state.provider_health.model_dump(mode="json"),
    }


def _format_provider_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:300]


def _save_replay_result(payload: dict, trade_date: str, strict: bool = True) -> Path:
    output_dir = PROJECT_DIR / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = _replay_mode(strict)
    output_path = output_dir / f"replay-today-{mode}-{trade_date or 'unknown'}.json"
    payload["artifact_path"] = str(output_path)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def _save_recent_replay_result(payload: dict, days: int, strict: bool = True, review: bool = False) -> Path:
    output_dir = PROJECT_DIR / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    day_values = [day.get("date", "") for day in payload.get("days", []) if isinstance(day, dict)]
    first_day = day_values[0] if day_values else "unknown"
    last_day = day_values[-1] if day_values else "unknown"
    mode = _replay_mode(strict)
    review_mode = _review_mode(review)
    output_path = output_dir / f"replay-recent-{days}d-{mode}-{review_mode}-{first_day}_{last_day}.json"
    payload["artifact_path"] = str(output_path)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def _load_saved_replay_result(strict: bool = True) -> dict | None:
    artifact_dir = PROJECT_DIR / "artifacts"
    if not artifact_dir.exists():
        return None

    mode = _replay_mode(strict)
    candidates = sorted(
        artifact_dir.glob(f"replay-today-{mode}-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("strict") is strict:
            payload["artifact_path"] = str(path)
            return payload
    return None


def _load_saved_recent_replay_result(days: int, strict: bool = True, review: bool = False) -> dict | None:
    artifact_dir = PROJECT_DIR / "artifacts"
    if not artifact_dir.exists():
        return None

    mode = _replay_mode(strict)
    review_mode = _review_mode(review)
    candidates = sorted(
        artifact_dir.glob(f"replay-recent-{days}d-{mode}-{review_mode}-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("strict") is strict
            and payload.get("review_enabled") is review
            and payload.get("schema_version") == 2
        ):
            payload["artifact_path"] = str(path)
            return payload
    return None


def _parse_trade_date(value: str) -> date_type:
    try:
        return date_type.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date, expected YYYY-MM-DD") from exc


def _fetch_and_cache_symbol_history(
    repo: PostgresRepository,
    provider: TencentMarketProvider,
    symbol: str,
) -> list[Candle]:
    candles = provider.fetch_minutes(symbol)
    repo.save_minute_bars(candles, source="tencent_ifzq")
    return candles


def _get_or_fetch_day_candles(
    repo: PostgresRepository,
    provider: TencentMarketProvider,
    symbol: str,
    trade_date: date_type,
) -> list[Candle]:
    candles = repo.get_minute_bars(symbol, trade_date)
    if candles:
        return candles

    try:
        fetched = _fetch_minutes_for_trade_date(provider, symbol, trade_date)
    except (MarketDataChannelUnavailable, httpx.HTTPError) as exc:
        raise HTTPException(
            status_code=503,
            detail=_market_data_error_detail(
                code="market_data_channel_unavailable",
                message=(
                    f"{symbol} {trade_date.isoformat()} 本地没有分钟线缓存，"
                    "行情源暂不可用，暂时无法补拉该日 1 分钟数据。"
                ),
                symbol=symbol,
                trade_date=trade_date,
                provider=provider,
                reason=_format_provider_error(exc),
            ),
        ) from exc
    except MarketDataUnavailable as exc:
        raise HTTPException(
            status_code=404,
            detail=_market_data_error_detail(
                code="minute_bars_not_found",
                message=(
                    f"{symbol} {trade_date.isoformat()} 本地没有分钟线缓存，"
                    "行情源也未返回该日 1 分钟数据。可能是非交易日、公开源不支持该历史范围，"
                    "或该日数据尚未入库。"
                ),
                symbol=symbol,
                trade_date=trade_date,
                provider=provider,
                reason=_format_provider_error(exc),
            ),
        ) from exc
    repo.save_minute_bars(fetched, source=_provider_source(provider))
    candles = [candle for candle in fetched if candle.symbol == symbol and candle.timestamp.date() == trade_date]
    if not candles:
        raise HTTPException(
            status_code=404,
            detail=_market_data_error_detail(
                code="minute_bars_not_found",
                message=(
                    f"{symbol} {trade_date.isoformat()} 行情源返回了数据，"
                    "但没有匹配到该股票当天交易分钟线。"
                ),
                symbol=symbol,
                trade_date=trade_date,
                provider=provider,
                reason="Fetched minute bars did not match requested symbol and date",
            ),
        )
    return sorted(candles, key=lambda candle: candle.timestamp)


def _context_day_candles(
    repo: PostgresRepository,
    provider: TencentMarketProvider,
    state: AppState,
    symbol: str,
    trade_date: date_type,
    candles: list[Candle],
) -> dict[str, list[Candle]]:
    candles_by_symbol = {symbol: candles}
    for item in state.watchlist:
        if item.symbol == symbol:
            continue
        try:
            candles_by_symbol[item.symbol] = _get_or_fetch_day_candles(repo, provider, item.symbol, trade_date)
        except HTTPException:
            continue
    for index_symbol in ("399006", "000300", "000001"):
        try:
            candles_by_symbol[index_symbol] = _get_or_fetch_day_candles(repo, provider, index_symbol, trade_date)
            break
        except HTTPException:
            continue
    return candles_by_symbol


def _fetch_minutes_for_trade_date(
    provider: TencentMarketProvider,
    symbol: str,
    trade_date: date_type,
) -> list[Candle]:
    fetch_for_date = getattr(provider, "fetch_minutes_for_date", None)
    if callable(fetch_for_date):
        return fetch_for_date(symbol, trade_date)
    return provider.fetch_minutes(symbol)


def _provider_source(provider: object) -> str:
    name = provider.__class__.__name__.lower()
    if "eastmoney" in name:
        return "eastmoney"
    if "akshare" in name:
        return "akshare"
    if "tencent" in name:
        return "tencent_ifzq"
    return "market_provider"


def _market_data_error_detail(
    *,
    code: str,
    message: str,
    symbol: str,
    trade_date: date_type,
    provider: object,
    reason: str,
) -> dict[str, str]:
    return {
        "code": code,
        "message": message,
        "symbol": symbol,
        "date": trade_date.isoformat(),
        "provider": _provider_source(provider),
        "reason": reason,
    }


def _day_payload(
    symbol: str,
    trade_date: date_type,
    candles: list[Candle],
    points: list,
    quote: MarketQuote | None,
) -> dict:
    sorted_candles = sorted(candles, key=lambda candle: candle.timestamp)
    return {
        "symbol": symbol,
        "date": trade_date.isoformat(),
        "chart_series": {
            "realtime": [candle.model_dump(mode="json") for candle in sorted_candles],
            "one_minute": [candle.model_dump(mode="json") for candle in sorted_candles],
            "five_minute": [
                candle.model_dump(mode="json") for candle in aggregate_five_minute(sorted_candles)
            ],
        },
        "points": [point.model_dump(mode="json") for point in points],
        "quote": quote.model_dump(mode="json") if quote else None,
        "provider_health": ProviderHealth(
            provider="postgres_cache",
            symbol=symbol,
            last_success_at=sorted_candles[-1].timestamp if sorted_candles else None,
            latency_ms=0,
        ).model_dump(mode="json"),
    }


class _StaticDayProvider:
    def __init__(self, candles_by_symbol: dict[str, list[Candle]]) -> None:
        self.candles_by_symbol = candles_by_symbol

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        return self.candles_by_symbol.get(symbol, [])


def _replay_mode(strict: bool) -> str:
    return "strict" if strict else "optimized"


def _review_mode(review: bool) -> str:
    return "reviewed" if review else "fast"


def _upsert_signal(state: AppState, signal: Signal) -> None:
    for index, existing in enumerate(state.signals):
        if existing.symbol == signal.symbol and existing.timestamp == signal.timestamp:
            state.signals[index] = signal
            return
    state.signals.append(signal)


def _review_candidate_signal(
    signal: Signal,
    candles: list[Candle],
    position: Position,
    state: AppState,
    reviewer: LlmReviewer,
    market_context: dict | None = None,
) -> Signal:
    if not signal.needs_llm_review:
        return signal

    import asyncio

    context = build_review_context(signal, candles, position, state.signals, market_context=market_context)
    return asyncio.run(reviewer.review(signal, context))


def _default_review_client() -> OpenAICompatibleClient:
    settings = get_settings()
    return OpenAICompatibleClient(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout_seconds=settings.openai_timeout_seconds,
        wire_api=settings.openai_wire_api,
        reasoning_effort=settings.openai_reasoning_effort,
        disable_response_storage=settings.openai_disable_response_storage,
    )


def _position_for_symbol(positions: list[Position], symbol: str) -> Position:
    for position in positions:
        if position.symbol == symbol:
            return position
    return Position(symbol=symbol, base_quantity=0, cost_price=0, available_cash=0, t_quantity=0)


def _ensure_watch_symbol(state: AppState, symbol: str) -> None:
    if not any(item.symbol == symbol for item in state.watchlist):
        raise HTTPException(status_code=404, detail="Symbol is not in watchlist")


def _recent_candles_by_symbol(state: AppState, limit: int) -> list[Candle]:
    candles: list[Candle] = []
    for item in state.watchlist:
        symbol_candles = [candle for candle in state.candles if candle.symbol == item.symbol]
        candles.extend(symbol_candles[-limit:])
    return candles


def _market_context_for_symbol(
    symbol: str,
    candles: list[Candle],
    candles_by_symbol: dict[str, list[Candle]],
):
    if not candles:
        return None
    latest_time = candles[-1].timestamp
    truncated = {
        item_symbol: [candle for candle in item_candles if candle.timestamp <= latest_time]
        for item_symbol, item_candles in candles_by_symbol.items()
    }
    index_symbol = _index_symbol_for_context(truncated)
    index_candles = truncated.get(index_symbol, []) if index_symbol else []
    sector_source = {
        item_symbol: item_candles
        for item_symbol, item_candles in truncated.items()
        if item_symbol not in _INDEX_SYMBOLS
    }
    sector_candles = build_equal_weight_sector_candles(symbol, sector_source)
    if not sector_candles:
        return None
    return build_market_context(candles, index_candles=index_candles, sector_candles=sector_candles)


_INDEX_SYMBOLS = {"399006", "000001", "000300"}


def _index_symbol_for_context(candles_by_symbol: dict[str, list[Candle]]) -> str | None:
    for symbol in ("399006", "000300", "000001"):
        if symbol in candles_by_symbol and candles_by_symbol[symbol]:
            return symbol
    return None


def _trading_day_candles_by_symbol(state: AppState) -> list[Candle]:
    candles: list[Candle] = []
    for item in state.watchlist:
        symbol_candles = sorted(
            [candle for candle in state.candles if candle.symbol == item.symbol],
            key=lambda candle: candle.timestamp,
        )
        if not symbol_candles:
            continue
        trade_date = symbol_candles[-1].timestamp.date()
        candles.extend(candle for candle in symbol_candles if candle.timestamp.date() == trade_date)
    return candles


def _five_minute_candles_by_symbol(state: AppState) -> list[Candle]:
    candles: list[Candle] = []
    for item in state.watchlist:
        symbol_candles = [candle for candle in state.candles if candle.symbol == item.symbol]
        candles.extend(aggregate_five_minute(symbol_candles))
    return candles


def _next_demo_candle(candles: list[Candle]) -> Candle:
    previous = candles[-1]
    index = len(candles)
    wave = [9.65, 9.82, 10.05, 10.25, 10.55, 10.78, 10.45, 10.22]
    close = wave[index % len(wave)]
    volume = [190, 220, 260, 320, 420, 520, 430, 300][index % 8]
    return Candle(
        symbol=previous.symbol,
        timestamp=previous.timestamp + timedelta(minutes=1),
        open=previous.close,
        high=max(previous.close, close) + 0.08,
        low=min(previous.close, close) - 0.08,
        close=close,
        volume=volume,
    )


app = create_app()
