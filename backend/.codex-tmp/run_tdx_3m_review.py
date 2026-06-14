from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from tmaker.config import PROJECT_DIR, get_settings  # noqa: E402
from tmaker.domain.models import Candle, Position  # noqa: E402
from tmaker.llm.openai_client import OpenAICompatibleClient  # noqa: E402
from tmaker.market.akshare_provider import MarketDataUnavailable  # noqa: E402
from tmaker.market.tdx_provider import TdxHistoricalMinuteProvider  # noqa: E402
from tmaker.storage.postgres import PostgresRepository  # noqa: E402
from tmaker.strategy.replay import RecentReplayResult, ReplayDayResult, replay_today  # noqa: E402


SYMBOLS = ["300502", "300308"]
SYMBOL_NAMES = {"300502": "新易盛", "300308": "中际旭创"}
LOOKBACK_DAYS = int(os.environ.get("TM_REVIEW_LOOKBACK_DAYS", "90"))
END_DATE = date.fromisoformat(os.environ.get("TM_REVIEW_END_DATE", "2026-06-12"))
START_DATE = date.fromisoformat(
    os.environ.get("TM_REVIEW_START_DATE", (END_DATE - timedelta(days=LOOKBACK_DAYS)).isoformat())
)
MAX_DAY_WORKERS = int(os.environ.get("TM_REVIEW_DAY_WORKERS", "2"))
MAX_FETCH_WORKERS = int(os.environ.get("TM_TDX_FETCH_WORKERS", "4"))
MIN_COMPLETE_DAY_ROWS = int(os.environ.get("TM_MIN_COMPLETE_DAY_ROWS", "240"))
REUSE_REVIEWED = os.environ.get("TM_REVIEW_REUSE_REVIEWED", "1") != "0"
QUANTITY = 100
MAX_ACTIONS_PER_SYMBOL_DAY = 4
MAX_PAIRS_PER_SYMBOL_DAY = 2
FEE_RATE = float(os.environ.get("TM_TRADE_FEE_RATE", "0.00025"))
MIN_FEE = float(os.environ.get("TM_TRADE_MIN_FEE", "5"))
STAMP_TAX_RATE = float(os.environ.get("TM_TRADE_STAMP_TAX_RATE", "0.0005"))
TRANSFER_FEE_RATE = float(os.environ.get("TM_TRADE_TRANSFER_FEE_RATE", "0.00001"))
POSITIONS = [
    Position(symbol="300502", base_quantity=400, cost_price=0, available_cash=300000, t_quantity=100),
    Position(symbol="300308", base_quantity=400, cost_price=0, available_cash=300000, t_quantity=100),
]


class StaticDayProvider:
    def __init__(self, candles_by_symbol: dict[str, list[Candle]]) -> None:
        self.candles_by_symbol = candles_by_symbol

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        return self.candles_by_symbol.get(symbol, [])


class LoggingReviewClient:
    def __init__(self, wrapped: OpenAICompatibleClient, cached_points: list[Any] | None = None) -> None:
        self.wrapped = wrapped
        self.attempts_by_key: Counter[tuple[str | None, str | None, str | None, str]] = Counter()
        self.cached_payloads = {
            (point.symbol, point.timestamp, point.action): {
                "action": point.llm_action,
                "confidence": point.llm_confidence,
                "summary": point.llm_summary or "",
                "reasons": point.llm_reasons,
                "risks": point.risks,
                "wait_for": point.wait_for,
                "execution_allowed": point.execution_allowed is not False,
                "execution_blockers": point.execution_blockers,
            }
            for point in cached_points or []
            if point.llm_status == "ok" and point.llm_action in {"buy", "sell", "hold"}
        }

    async def create_review(self, context: dict) -> dict:
        candidate = context.get("candidate") or {}
        key = (
            context.get("symbol"),
            context.get("timestamp"),
            candidate.get("action"),
            ",".join(candidate.get("rule_ids") or []),
        )
        cached_key = (context.get("symbol"), context.get("timestamp"), candidate.get("action"))
        if cached_key in self.cached_payloads:
            payload = self.cached_payloads[cached_key]
            print(
                json.dumps(
                    {
                        "event": "review_reuse",
                        "symbol": context.get("symbol"),
                        "timestamp": context.get("timestamp"),
                        "action": candidate.get("action"),
                        "llm_action": payload.get("action"),
                        "confidence": payload.get("confidence"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return payload
        self.attempts_by_key[key] += 1
        attempt = self.attempts_by_key[key]
        started = time.time()
        print(
            json.dumps(
                {
                    "event": "review_start",
                    "symbol": context.get("symbol"),
                    "timestamp": context.get("timestamp"),
                    "action": candidate.get("action"),
                    "rule_ids": candidate.get("rule_ids") or [],
                    "attempt": attempt,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            payload = await self.wrapped.create_review(context)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "review_error",
                        "symbol": context.get("symbol"),
                        "timestamp": context.get("timestamp"),
                        "action": candidate.get("action"),
                        "attempt": attempt,
                        "elapsed_seconds": round(time.time() - started, 2),
                        "error": str(exc)[:300],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            raise
        print(
            json.dumps(
                {
                    "event": "review_done",
                    "symbol": context.get("symbol"),
                    "timestamp": context.get("timestamp"),
                    "action": candidate.get("action"),
                    "attempt": attempt,
                    "elapsed_seconds": round(time.time() - started, 2),
                    "llm_action": payload.get("action"),
                    "confidence": payload.get("confidence"),
                    "execution_allowed": payload.get("execution_allowed"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return payload


@dataclass(frozen=True)
class Pair:
    date: str
    symbol: str
    name: str
    order: str
    buy_time: str
    buy_price: float
    buy_confidence: float
    sell_time: str
    sell_price: float
    sell_confidence: float
    spread: float
    spread_pct: float
    gross_pnl: float
    estimated_fees: float
    net_pnl: float


def main() -> int:
    started = time.time()
    settings = get_settings()
    repo = PostgresRepository(settings.database_url)
    repo.init_schema()

    fetch_summary = fetch_and_cache_tdx(repo)
    trade_dates = cached_trade_dates(repo)
    if not trade_dates:
        print("No common cached trade dates after TDX fetch", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "event": "review_range",
                "start": trade_dates[0].isoformat(),
                "end": trade_dates[-1].isoformat(),
                "trading_day_count": len(trade_dates),
                "symbols": SYMBOLS,
                "max_day_workers": MAX_DAY_WORKERS,
                "reuse_reviewed": REUSE_REVIEWED,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    day_results: list[ReplayDayResult] = []
    with ThreadPoolExecutor(max_workers=MAX_DAY_WORKERS) as executor:
        futures = {
            executor.submit(run_day, index, len(trade_dates), trade_date): trade_date
            for index, trade_date in enumerate(trade_dates, start=1)
        }
        for future in as_completed(futures):
            day_results.append(future.result())
    day_results.sort(key=lambda item: item.date)

    result = RecentReplayResult(
        days_requested=len(day_results),
        mode="strict",
        strict=True,
        review_enabled=True,
        symbols=SYMBOLS,
        days=day_results,
        summary=combine_summary(day_results),
    )
    payload = result.model_dump(mode="json")
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["range"] = {
        "start": trade_dates[0].isoformat(),
        "end": trade_dates[-1].isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "requested_start": START_DATE.isoformat(),
        "requested_end": END_DATE.isoformat(),
    }
    payload["fetch_summary"] = fetch_summary
    payload["symbol_summary"] = symbol_summary(payload)
    pair_summary = build_pair_summary(payload)
    payload["pair_summary"] = pair_summary
    payload["elapsed_seconds"] = round(time.time() - started, 2)

    output_dir = PROJECT_DIR / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{trade_dates[0].isoformat()}_{trade_dates[-1].isoformat()}"
    result_path = output_dir / f"tdx-3m-ai-replay-300502-300308-{suffix}.json"
    summary_path = output_dir / f"tdx-3m-ai-replay-300502-300308-{suffix}-summary.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "artifact_path": str(result_path),
        "summary_path": str(summary_path),
        "range": payload["range"],
        "elapsed_seconds": payload["elapsed_seconds"],
        "fetch_summary": fetch_summary,
        "summary": payload["summary"],
        "symbol_summary": payload["symbol_summary"],
        "pair_summary": pair_summary,
        "failed_points": [
            point_brief(point)
            for day in payload["days"]
            for point in day["points"]
            if point["llm_status"] == "failed"
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"event": "done", **summary}, ensure_ascii=False), flush=True)
    return 0


def export_from_database() -> dict:
    settings = get_settings()
    repo = PostgresRepository(settings.database_url)
    repo.init_schema()
    trade_dates = cached_trade_dates(repo)
    output_days = []
    for trade_date in trade_dates:
        candles_by_symbol = {symbol: repo.get_minute_bars(symbol, trade_date) for symbol in SYMBOLS}
        candidate_result = replay_today(StaticDayProvider(candles_by_symbol), SYMBOLS, POSITIONS, review_client=None)
        points = [
            point
            for symbol in SYMBOLS
            for point in repo.get_replay_points(symbol, trade_date, strict=True)
        ]
        output_days.append(
            ReplayDayResult(
                date=trade_date.isoformat(),
                mode=candidate_result.mode,
                strict=candidate_result.strict,
                chart_series=day_chart_series(candles_by_symbol),
                points=points,
                summary=with_accuracy_summary(candidate_result.summary, points, candles_by_symbol),
            )
        )
    result = RecentReplayResult(
        days_requested=len(output_days),
        mode="strict",
        strict=True,
        review_enabled=True,
        symbols=SYMBOLS,
        days=output_days,
        summary=combine_summary(output_days),
    )
    payload = result.model_dump(mode="json")
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["range"] = {
        "start": trade_dates[0].isoformat() if trade_dates else START_DATE.isoformat(),
        "end": trade_dates[-1].isoformat() if trade_dates else END_DATE.isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "requested_start": START_DATE.isoformat(),
        "requested_end": END_DATE.isoformat(),
    }
    payload["symbol_summary"] = symbol_summary(payload)
    payload["pair_summary"] = build_pair_summary(payload)
    return payload


def write_payload_artifacts(payload: dict, *, prefix: str = "tdx-3m-ai-replay") -> tuple[Path, Path]:
    output_dir = PROJECT_DIR / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{payload['range']['start']}_{payload['range']['end']}"
    result_path = output_dir / f"{prefix}-300502-300308-{suffix}.json"
    summary_path = output_dir / f"{prefix}-300502-300308-{suffix}-summary.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "artifact_path": str(result_path),
        "summary_path": str(summary_path),
        "range": payload["range"],
        "summary": payload["summary"],
        "symbol_summary": payload["symbol_summary"],
        "pair_summary": payload["pair_summary"],
        "failed_points": [
            point_brief(point)
            for day in payload["days"]
            for point in day["points"]
            if point["llm_status"] == "failed"
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_path, summary_path


def fetch_and_cache_tdx(repo: PostgresRepository) -> dict:
    provider = TdxHistoricalMinuteProvider(timeout=8)
    weekdays = [START_DATE + timedelta(days=offset) for offset in range((END_DATE - START_DATE).days + 1)]
    weekdays = [day for day in weekdays if day.weekday() < 5]
    tasks = [(symbol, day) for symbol in SYMBOLS for day in weekdays]
    fetched_count = 0
    skipped_cached = 0
    partial_count = 0
    empty_count = 0
    error_count = 0
    rows_by_symbol: Counter[str] = Counter()
    days_by_symbol: dict[str, set[str]] = defaultdict(set)

    print(
        json.dumps(
            {
                "event": "fetch_start",
                "source": "tdx",
                "requested_start": START_DATE.isoformat(),
                "requested_end": END_DATE.isoformat(),
                "weekday_symbol_days": len(tasks),
                "max_fetch_workers": MAX_FETCH_WORKERS,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    def fetch_one(symbol: str, trade_date: date) -> tuple[str, str, int, str]:
        cached = repo.get_minute_bars(symbol, trade_date)
        if len(cached) >= MIN_COMPLETE_DAY_ROWS:
            return symbol, trade_date.isoformat(), len(cached), "cached"
        try:
            candles = provider.fetch_minutes_for_date(symbol, trade_date)
        except MarketDataUnavailable:
            return symbol, trade_date.isoformat(), 0, "empty"
        except Exception as exc:
            return symbol, trade_date.isoformat(), 0, f"error:{str(exc)[:160]}"
        repo.save_minute_bars(candles, source="tdx")
        status = "fetched" if len(candles) >= MIN_COMPLETE_DAY_ROWS else "partial"
        return symbol, trade_date.isoformat(), len(candles), status

    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        futures = [executor.submit(fetch_one, symbol, trade_date) for symbol, trade_date in tasks]
        for future in as_completed(futures):
            symbol, day, rows, status = future.result()
            if status == "cached":
                skipped_cached += 1
            elif status == "fetched":
                fetched_count += 1
            elif status == "partial":
                partial_count += 1
            elif status == "empty":
                empty_count += 1
            elif status.startswith("error:"):
                error_count += 1
            if rows:
                rows_by_symbol[symbol] += rows
                days_by_symbol[symbol].add(day)
            print(
                json.dumps(
                    {"event": "fetch_day", "symbol": symbol, "date": day, "rows": rows, "status": status},
                    ensure_ascii=False,
                ),
                flush=True,
            )

    summary = {
        "source": "tdx",
        "requested_start": START_DATE.isoformat(),
        "requested_end": END_DATE.isoformat(),
        "fetched_symbol_days": fetched_count,
        "cached_symbol_days": skipped_cached,
        "partial_symbol_days": partial_count,
        "empty_symbol_days": empty_count,
        "error_symbol_days": error_count,
        "rows_by_symbol": dict(rows_by_symbol),
        "days_by_symbol": {symbol: len(days) for symbol, days in days_by_symbol.items()},
    }
    print(json.dumps({"event": "fetch_done", **summary}, ensure_ascii=False), flush=True)
    return summary


def cached_trade_dates(repo: PostgresRepository) -> list[date]:
    complete_days = []
    for day_text in sorted(set.intersection(*(set(repo.list_trading_days(symbol)) for symbol in SYMBOLS))):
        trade_date = date.fromisoformat(day_text)
        if not START_DATE <= trade_date <= END_DATE:
            continue
        if all(len(repo.get_minute_bars(symbol, trade_date)) >= MIN_COMPLETE_DAY_ROWS for symbol in SYMBOLS):
            complete_days.append(trade_date)
    return complete_days


def run_day(index: int, total: int, trade_date: date) -> ReplayDayResult:
    settings = get_settings()
    repo = PostgresRepository(settings.database_url)
    day_started = time.time()
    candles_by_symbol = {symbol: repo.get_minute_bars(symbol, trade_date) for symbol in SYMBOLS}
    print(
        json.dumps(
            {
                "event": "day_start",
                "index": index,
                "total": total,
                "date": trade_date.isoformat(),
                "rows": {symbol: len(candles) for symbol, candles in candles_by_symbol.items()},
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    reused_points = existing_reviewed_points(repo, trade_date)
    if REUSE_REVIEWED and reused_points is not None:
        candidate_result = replay_today(StaticDayProvider(candles_by_symbol), SYMBOLS, POSITIONS, review_client=None)
        day = ReplayDayResult(
            date=trade_date.isoformat(),
            mode=candidate_result.mode,
            strict=candidate_result.strict,
            chart_series=day_chart_series(candles_by_symbol),
            points=reused_points,
            summary=with_accuracy_summary(candidate_result.summary, reused_points, candles_by_symbol),
        )
        print(
            json.dumps(
                {
                    "event": "day_reused",
                    "index": index,
                    "total": total,
                    "date": trade_date.isoformat(),
                    "elapsed_seconds": round(time.time() - day_started, 2),
                    "summary": day.summary,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return day

    client = LoggingReviewClient(
        OpenAICompatibleClient(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            timeout_seconds=settings.openai_timeout_seconds,
            wire_api=settings.openai_wire_api,
            reasoning_effort=settings.openai_reasoning_effort,
            disable_response_storage=settings.openai_disable_response_storage,
        ),
        cached_points=[
            point
            for symbol in SYMBOLS
            for point in repo.get_replay_points(symbol, trade_date, strict=True)
        ],
    )
    result = replay_today(StaticDayProvider(candles_by_symbol), SYMBOLS, POSITIONS, review_client=client)
    for symbol in SYMBOLS:
        repo.replace_replay_points_for_day(
            symbol,
            trade_date,
            [point for point in result.points if point.symbol == symbol],
            strict=True,
        )
    day = ReplayDayResult(
        date=trade_date.isoformat(),
        mode=result.mode,
        strict=result.strict,
        chart_series=day_chart_series(candles_by_symbol),
        points=result.points,
        summary=with_accuracy_summary(result.summary, result.points, candles_by_symbol),
    )
    print(
        json.dumps(
            {
                "event": "day_done",
                "index": index,
                "total": total,
                "date": trade_date.isoformat(),
                "elapsed_seconds": round(time.time() - day_started, 2),
                "summary": day.summary,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return day


def existing_reviewed_points(repo: PostgresRepository, trade_date: date):
    points = []
    for symbol in SYMBOLS:
        symbol_points = repo.get_replay_points(symbol, trade_date, strict=True)
        if not symbol_points:
            return None
        points.extend(symbol_points)
    if not points:
        return None
    if any(point.llm_status == "failed" for point in points):
        return None
    if not any(point.llm_status == "ok" for point in points):
        return None
    return points


def day_chart_series(candles_by_symbol: dict[str, list[Candle]]) -> dict[str, list[Candle]]:
    return {
        "realtime": [candle for candles in candles_by_symbol.values() for candle in candles],
        "one_minute": [candle for candles in candles_by_symbol.values() for candle in candles],
        "five_minute": [],
    }


def with_accuracy_summary(base_summary: dict, points: list[Any], candles_by_symbol: dict[str, list[Candle]]) -> dict:
    checked = [point_accuracy(point, candles_by_symbol.get(point.symbol, [])) for point in points]
    checked = [item for item in checked if item is not None]
    hit_count = sum(1 for item in checked if item)
    total = len(checked)
    return {
        **base_summary,
        "reviewed_count": sum(1 for point in points if point.llm_status == "ok"),
        "ai_buy_count": sum(1 for point in points if point.llm_action == "buy"),
        "ai_sell_count": sum(1 for point in points if point.llm_action == "sell"),
        "ai_hold_count": sum(1 for point in points if point.llm_action == "hold"),
        "ai_failed_count": sum(1 for point in points if point.llm_status == "failed"),
        "accuracy_checked_count": total,
        "accuracy_hit_count": hit_count,
        "accuracy_rate_pct": round(hit_count / total * 100, 2) if total else None,
    }


def combine_summary(days: list[ReplayDayResult]) -> dict:
    totals: Counter[str] = Counter()
    for day in days:
        for key, value in day.summary.items():
            if isinstance(value, int):
                totals[key] += value
    checked = totals["accuracy_checked_count"]
    hit_count = totals["accuracy_hit_count"]
    return {
        "trading_day_count": len(days),
        "candidate_count": totals["candidate_count"],
        "buy_count": totals["buy_count"],
        "sell_count": totals["sell_count"],
        "reviewed_count": totals["reviewed_count"],
        "ai_buy_count": totals["ai_buy_count"],
        "ai_sell_count": totals["ai_sell_count"],
        "ai_hold_count": totals["ai_hold_count"],
        "ai_failed_count": totals["ai_failed_count"],
        "accuracy_checked_count": checked,
        "accuracy_hit_count": hit_count,
        "accuracy_rate_pct": round(hit_count / checked * 100, 2) if checked else None,
    }


def symbol_summary(payload: dict) -> dict:
    by_symbol = {}
    for symbol in SYMBOLS:
        points = [point for day in payload["days"] for point in day["points"] if point["symbol"] == symbol]
        checked = [point_accuracy_dict(point, payload["days"]) for point in points]
        checked = [item for item in checked if item is not None]
        hits = sum(1 for item in checked if item)
        eligible = [point for point in points if eligible_point(point)]
        by_symbol[symbol] = {
            "name": SYMBOL_NAMES[symbol],
            "point_count": len(points),
            "eligible_point_count": len(eligible),
            "candidate_buy_count": sum(1 for point in points if point["action"] == "buy"),
            "candidate_sell_count": sum(1 for point in points if point["action"] == "sell"),
            "reviewed_count": sum(1 for point in points if point["llm_status"] == "ok"),
            "failed_count": sum(1 for point in points if point["llm_status"] == "failed"),
            "ai_buy_count": sum(1 for point in points if point["llm_action"] == "buy"),
            "ai_sell_count": sum(1 for point in points if point["llm_action"] == "sell"),
            "ai_hold_count": sum(1 for point in points if point["llm_action"] == "hold"),
            "accuracy_checked_count": len(checked),
            "accuracy_hit_count": hits,
            "accuracy_rate_pct": round(hits / len(checked) * 100, 2) if checked else None,
        }
    return by_symbol


def build_pair_summary(payload: dict) -> dict:
    points = [
        normalize_point(point)
        for day in payload["days"]
        for point in day["points"]
        if eligible_point(point)
    ]
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for point in points:
        grouped[(point["symbol"], point["date"])].append(point)
    pairs: list[Pair] = []
    for key in sorted(grouped):
        pairs.extend(open_reprice_directional_close_watch_pairs(grouped[key]))
    by_symbol = {}
    for symbol in SYMBOLS:
        symbol_pairs = [pair for pair in pairs if pair.symbol == symbol]
        by_symbol[symbol] = aggregate_pairs(symbol_pairs) | {"name": SYMBOL_NAMES[symbol]}
    return {
        "strategy": "stock_specific_open_reprice_directional_close_watch",
        "quantity_per_action": QUANTITY,
        "max_actions_per_symbol_day": MAX_ACTIONS_PER_SYMBOL_DAY,
        "max_pairs_per_symbol_day": MAX_PAIRS_PER_SYMBOL_DAY,
        "fee_model": {
            "commission_rate": FEE_RATE,
            "min_commission_per_side": MIN_FEE,
            "stamp_tax_sell_rate": STAMP_TAX_RATE,
            "transfer_fee_rate_per_side": TRANSFER_FEE_RATE,
        },
        **aggregate_pairs(pairs),
        "by_symbol": by_symbol,
        "pairs": [pair.__dict__ for pair in pairs],
    }


def open_reprice_directional_close_watch_pairs(points: list[dict]) -> list[Pair]:
    if not points:
        return []
    symbol = points[0]["symbol"]
    points = stock_specific_filter(points)
    pending_open = None
    pairs = []
    action_count = 0
    for point in sorted(points, key=lambda item: item["timestamp"]):
        if action_count >= MAX_ACTIONS_PER_SYMBOL_DAY or len(pairs) >= MAX_PAIRS_PER_SYMBOL_DAY:
            break
        if pending_open is None:
            pending_open = point
            action_count += 1
            continue
        if point["llm_action"] == pending_open["llm_action"]:
            if better_open(point, pending_open):
                pending_open = point
            continue
        pair = make_pair(pending_open, point)
        if should_close_tracked_pair(pair):
            pairs.append(pair)
            pending_open = None
            action_count += 1
    return pairs


def stock_specific_filter(points: list[dict]) -> list[dict]:
    if not points or points[0]["symbol"] != "300502":
        return points
    return [point for point in points if not neway_weak_buy_guard(point)]


def neway_weak_buy_guard(point: dict) -> bool:
    if point.get("llm_action") != "buy":
        return False
    rule_ids = set(point.get("rule_ids") or [])
    return "deep_session_vwap_low_buy" in rule_ids and point.get("price_session_vwap_deviation_pct", 0) > -1.45


def make_pair(left: dict, right: dict) -> Pair:
    if left["llm_action"] == "buy":
        buy = left
        sell = right
        order = "buy->sell"
    else:
        buy = right
        sell = left
        order = "sell->buy"
    spread = round(sell["price"] - buy["price"], 4)
    reference_price = buy["price"] if order == "buy->sell" else sell["price"]
    spread_pct = round(spread / reference_price * 100, 4) if reference_price else 0
    gross_pnl = round(spread * QUANTITY, 2)
    fees = estimate_pair_fees(buy["price"], sell["price"])
    return Pair(
        date=buy["date"],
        symbol=buy["symbol"],
        name=SYMBOL_NAMES.get(buy["symbol"], buy["symbol"]),
        order=order,
        buy_time=buy["time"],
        buy_price=buy["price"],
        buy_confidence=buy["llm_confidence"],
        sell_time=sell["time"],
        sell_price=sell["price"],
        sell_confidence=sell["llm_confidence"],
        spread=spread,
        spread_pct=spread_pct,
        gross_pnl=gross_pnl,
        estimated_fees=fees,
        net_pnl=round(gross_pnl - fees, 2),
    )


def estimate_pair_fees(buy_price: float, sell_price: float) -> float:
    buy_amount = buy_price * QUANTITY
    sell_amount = sell_price * QUANTITY
    buy_commission = max(buy_amount * FEE_RATE, MIN_FEE)
    sell_commission = max(sell_amount * FEE_RATE, MIN_FEE)
    stamp_tax = sell_amount * STAMP_TAX_RATE
    transfer_fee = (buy_amount + sell_amount) * TRANSFER_FEE_RATE
    return round(buy_commission + sell_commission + stamp_tax + transfer_fee, 2)


def aggregate_pairs(pairs: list[Pair]) -> dict:
    success_count = sum(1 for pair in pairs if pair.net_pnl > 0)
    gross = sum(pair.gross_pnl for pair in pairs)
    fees = sum(pair.estimated_fees for pair in pairs)
    net = sum(pair.net_pnl for pair in pairs)
    spread_sum = sum(pair.spread for pair in pairs)
    return {
        "paired_trade_count": len(pairs),
        "success_count": success_count,
        "success_rate_pct": round(success_count / len(pairs) * 100, 2) if pairs else 0,
        "total_pnl_gross": round(gross, 2),
        "total_estimated_fees": round(fees, 2),
        "total_pnl_net": round(net, 2),
        "average_spread_per_pair": round(spread_sum / len(pairs), 4) if pairs else 0,
        "average_gross_pnl_per_pair": round(gross / len(pairs), 2) if pairs else 0,
        "average_fee_per_pair": round(fees / len(pairs), 2) if pairs else 0,
        "average_net_pnl_per_pair": round(net / len(pairs), 2) if pairs else 0,
    }


def better_open(candidate: dict, current: dict) -> bool:
    if candidate["llm_action"] == "buy":
        return candidate["price"] < current["price"]
    if candidate["llm_action"] == "sell":
        return candidate["price"] > current["price"]
    return False


def should_close_tracked_pair(pair: Pair) -> bool:
    close_time = pair.sell_time if pair.order == "buy->sell" else pair.buy_time
    if pair.order == "sell->buy":
        return pair.spread_pct >= 1.0 or (close_time >= "14:30" and pair.spread > 0)
    return (
        pair.spread_pct >= 3.0
        or (close_time >= "13:00" and pair.spread_pct >= 1.0)
        or (close_time >= "14:30" and pair.spread > 0)
    )


def eligible_point(point: dict) -> bool:
    if point.get("llm_status") != "ok" or point.get("execution_allowed") is not True:
        return False
    action = point.get("llm_action")
    confidence = point.get("llm_confidence") or 0
    return (action == "sell" and confidence > 0.58) or (action == "buy" and confidence >= 0.54)


def normalize_point(point: dict) -> dict:
    timestamp = datetime.fromisoformat(point["timestamp"])
    return {
        **point,
        "date": timestamp.date().isoformat(),
        "time": timestamp.strftime("%H:%M"),
        "price": float(point["price"]),
        "llm_confidence": float(point.get("llm_confidence") or 0),
    }


def point_accuracy(point: Any, candles: list[Candle]) -> bool | None:
    action = point.llm_action or point.action
    if action not in {"buy", "sell"}:
        return None
    return point_accuracy_payload(
        point.timestamp,
        action,
        point.price,
        [candle.model_dump(mode="json") for candle in candles],
    )


def point_accuracy_dict(point: dict, days: list[dict]) -> bool | None:
    action = point.get("llm_action") or point.get("action")
    if action not in {"buy", "sell"}:
        return None
    day = next((item for item in days if item["date"] == point["timestamp"][:10]), None)
    if not day:
        return None
    candles = [candle for candle in day["chart_series"]["one_minute"] if candle["symbol"] == point["symbol"]]
    return point_accuracy_payload(point["timestamp"], action, point["price"], candles)


def point_accuracy_payload(timestamp_text: str, action: str, price: float, candles: list[dict]) -> bool | None:
    timestamp = datetime.fromisoformat(timestamp_text)
    future = [
        candle
        for candle in sorted(candles, key=lambda item: item["timestamp"])
        if 0 < (datetime.fromisoformat(candle["timestamp"]) - timestamp).total_seconds() <= 30 * 60
    ]
    if not future:
        return None
    if action == "buy":
        best = min(float(candle["low"]) for candle in future)
        rebound = max(float(candle["high"]) for candle in future)
        return rebound >= price * 1.005 or best >= price * 0.997
    best = max(float(candle["high"]) for candle in future)
    pullback = min(float(candle["low"]) for candle in future)
    return pullback <= price * 0.995 or best <= price * 1.003


def point_brief(point: dict) -> dict:
    return {
        "symbol": point["symbol"],
        "timestamp": point["timestamp"],
        "action": point["action"],
        "price": point["price"],
        "rule_ids": point["rule_ids"],
        "execution_blockers": point["execution_blockers"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
