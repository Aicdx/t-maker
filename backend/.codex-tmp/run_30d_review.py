from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time

from tmaker.config import PROJECT_DIR, get_settings
from tmaker.domain.models import Position
from tmaker.llm.openai_client import OpenAICompatibleClient
from tmaker.storage.postgres import PostgresRepository
from tmaker.strategy.replay import RecentReplayResult, ReplayDayResult, replay_today


SYMBOLS = ["300502", "300308"]
MAX_DAY_WORKERS = int(os.environ.get("TM_REVIEW_DAY_WORKERS", "4"))
REUSE_REVIEWED_DATES = {
    date.fromisoformat(value)
    for value in os.environ.get("TM_REVIEW_REUSE_DATES", "").split(",")
    if value.strip()
}
POSITIONS = [
    Position(symbol="300502", base_quantity=200, cost_price=0, available_cash=200000, t_quantity=100),
    Position(symbol="300308", base_quantity=200, cost_price=0, available_cash=200000, t_quantity=100),
]


class StaticDayProvider:
    def __init__(self, candles_by_symbol):
        self.candles_by_symbol = candles_by_symbol

    def fetch_minutes(self, symbol: str):
        return self.candles_by_symbol.get(symbol, [])


class LoggingReviewClient:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.attempts_by_key = Counter()

    async def create_review(self, context: dict) -> dict:
        candidate = context.get("candidate") or {}
        key = (
            context.get("symbol"),
            context.get("timestamp"),
            candidate.get("action"),
            ",".join(candidate.get("rule_ids") or []),
        )
        self.attempts_by_key[key] += 1
        attempt = self.attempts_by_key[key]
        started = time.time()
        print(
            json.dumps(
                {
                    "event": "review_attempt_start",
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
                        "event": "review_attempt_error",
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
                    "event": "review_attempt_done",
                    "symbol": context.get("symbol"),
                    "timestamp": context.get("timestamp"),
                    "action": candidate.get("action"),
                    "attempt": attempt,
                    "elapsed_seconds": round(time.time() - started, 2),
                    "llm_action": payload.get("action"),
                    "confidence": payload.get("confidence"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return payload


def main() -> int:
    settings = get_settings()
    repo = PostgresRepository(settings.database_url)
    repo.init_schema()
    all_days = sorted({date.fromisoformat(day) for symbol in SYMBOLS for day in repo.list_trading_days(symbol)})
    if not all_days:
        print("No cached trading days found", file=sys.stderr)
        return 1

    latest = all_days[-1]
    start = latest - timedelta(days=30)
    trade_dates = [
        day
        for day in all_days
        if day >= start and all(day.isoformat() in repo.list_trading_days(symbol) for symbol in SYMBOLS)
    ]
    output_days: list[ReplayDayResult] = []
    started = time.time()
    print(
        json.dumps(
            {
                "event": "start",
                "symbols": SYMBOLS,
                "latest": latest.isoformat(),
                "start": start.isoformat(),
                "trading_day_count": len(trade_dates),
                "trade_dates": [day.isoformat() for day in trade_dates],
                "max_day_workers": MAX_DAY_WORKERS,
                "reuse_reviewed_dates": [day.isoformat() for day in sorted(REUSE_REVIEWED_DATES)],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=MAX_DAY_WORKERS) as executor:
        futures = {
            executor.submit(_run_day, index, len(trade_dates), trade_date): trade_date
            for index, trade_date in enumerate(trade_dates, start=1)
        }
        for future in as_completed(futures):
            output_days.append(future.result())

    output_days.sort(key=lambda day: day.date)

    result = RecentReplayResult(
        days_requested=30,
        mode="strict",
        strict=True,
        review_enabled=True,
        symbols=SYMBOLS,
        days=output_days,
        summary=_combine_summary(output_days),
    )
    payload = result.model_dump(mode="json")
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["date_range"] = {
        "latest_cached_trade_date": latest.isoformat(),
        "natural_start_date": start.isoformat(),
        "covered_trading_days": [day.isoformat() for day in trade_dates],
    }
    payload["elapsed_seconds"] = round(time.time() - started, 2)
    payload["symbol_summary"] = _symbol_summary(payload)

    output_dir = PROJECT_DIR / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{trade_dates[0].isoformat()}_{trade_dates[-1].isoformat()}" if trade_dates else "none"
    result_path = output_dir / f"replay-30d-300502-300308-reviewed-{suffix}.json"
    summary_path = output_dir / f"replay-30d-300502-300308-reviewed-{suffix}-summary.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "artifact_path": str(result_path),
        "summary_path": str(summary_path),
        "date_range": payload["date_range"],
        "elapsed_seconds": payload["elapsed_seconds"],
        "summary": payload["summary"],
        "symbol_summary": payload["symbol_summary"],
        "day_summary": [
            {"date": day["date"], **day["summary"]}
            for day in payload["days"]
        ],
        "failed_points": [
            _point_brief(point)
            for day in payload["days"]
            for point in day["points"]
            if point["llm_status"] == "failed"
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"event": "done", **summary}, ensure_ascii=False), flush=True)
    return 0


def _run_day(index: int, total: int, trade_date: date) -> ReplayDayResult:
    settings = get_settings()
    repo = PostgresRepository(settings.database_url)
    day_started = time.time()
    print(
        json.dumps(
            {
                "event": "day_start",
                "index": index,
                "total": total,
                "date": trade_date.isoformat(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    candles_by_symbol = {symbol: repo.get_minute_bars(symbol, trade_date) for symbol in SYMBOLS}
    if trade_date in REUSE_REVIEWED_DATES:
        points = [
            point
            for symbol in SYMBOLS
            for point in repo.get_replay_points(symbol, trade_date, strict=True)
        ]
        candidate_result = replay_today(StaticDayProvider(candles_by_symbol), SYMBOLS, POSITIONS, review_client=None, strict=True)
        day = ReplayDayResult(
            date=trade_date.isoformat(),
            mode=candidate_result.mode,
            strict=candidate_result.strict,
            chart_series=_day_chart_series(candles_by_symbol),
            points=points,
            summary=_with_accuracy_summary(candidate_result.summary, points, candles_by_symbol),
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
        )
    )
    result = replay_today(StaticDayProvider(candles_by_symbol), SYMBOLS, POSITIONS, review_client=client, strict=True)
    day = ReplayDayResult(
        date=trade_date.isoformat(),
        mode=result.mode,
        strict=result.strict,
        chart_series=_day_chart_series(candles_by_symbol),
        points=result.points,
        summary=_with_accuracy_summary(result.summary, result.points, candles_by_symbol),
    )
    for symbol in SYMBOLS:
        repo.replace_replay_points_for_day(
            symbol,
            trade_date,
            [point for point in result.points if point.symbol == symbol],
            strict=True,
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


def _day_chart_series(candles_by_symbol):
    return {
        "realtime": [candle for candles in candles_by_symbol.values() for candle in candles],
        "one_minute": [candle for candles in candles_by_symbol.values() for candle in candles],
        "five_minute": [],
    }


def _with_accuracy_summary(base_summary, points, candles_by_symbol):
    checked = [_point_accuracy(point, candles_by_symbol.get(point.symbol, [])) for point in points]
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


def _combine_summary(days):
    totals = Counter()
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


def _symbol_summary(payload):
    by_symbol = {}
    for symbol in SYMBOLS:
        points = [
            point
            for day in payload["days"]
            for point in day["points"]
            if point["symbol"] == symbol
        ]
        checked = [_point_accuracy_dict(point, payload["days"]) for point in points]
        checked = [item for item in checked if item is not None]
        hits = sum(1 for item in checked if item)
        by_symbol[symbol] = {
            "point_count": len(points),
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


def _point_accuracy_dict(point, days):
    action = point["llm_action"] or point["action"]
    if action not in {"buy", "sell"}:
        return None
    day = next((item for item in days if item["date"] == point["timestamp"][:10]), None)
    if not day:
        return None
    candles = [candle for candle in day["chart_series"]["one_minute"] if candle["symbol"] == point["symbol"]]
    return _point_accuracy_payload(point["timestamp"], action, point["price"], candles)


def _point_accuracy(point, candles):
    action = point.llm_action or point.action
    if action not in {"buy", "sell"}:
        return None
    return _point_accuracy_payload(point.timestamp, action, point.price, [candle.model_dump(mode="json") for candle in candles])


def _point_accuracy_payload(timestamp_text, action, price, candles):
    timestamp = datetime.fromisoformat(timestamp_text)
    future = [
        candle
        for candle in sorted(candles, key=lambda item: item["timestamp"])
        if 0 < (datetime.fromisoformat(candle["timestamp"]) - timestamp).total_seconds() <= 30 * 60
    ]
    if not future:
        return None
    if action == "buy":
        best = min(candle["low"] for candle in future)
        rebound = max(candle["high"] for candle in future)
        return rebound >= price * 1.005 or best >= price * 0.997
    best = max(candle["high"] for candle in future)
    pullback = min(candle["low"] for candle in future)
    return pullback <= price * 0.995 or best <= price * 1.003


def _point_brief(point):
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
