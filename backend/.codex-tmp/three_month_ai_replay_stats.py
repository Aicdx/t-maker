from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "artifacts" / "ai-replay-300502-300308-3m-trade-stats.json"

QUANTITY = 100
MAX_ACTIONS_PER_SYMBOL_DAY = 4
MAX_PAIRS_PER_SYMBOL_DAY = MAX_ACTIONS_PER_SYMBOL_DAY // 2


@dataclass(frozen=True)
class Point:
    date: str
    symbol: str
    timestamp: str
    time: str
    action: str
    price: float
    confidence: float
    rule_ids: list[str]
    llm_summary: str | None


@dataclass(frozen=True)
class Pair:
    date: str
    symbol: str
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
    fees: float
    net_pnl: float


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize 3-month AI replay T-trade performance.")
    parser.add_argument("artifact", type=Path, help="AI replay JSON, either top-level points or recent-replay days.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--buy-confidence", type=float, default=0.54)
    parser.add_argument("--sell-confidence", type=float, default=0.58)
    parser.add_argument("--quantity", type=int, default=QUANTITY)
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--stamp-duty-rate", type=float, default=0.0005)
    parser.add_argument("--transfer-fee-rate", type=float, default=0.00001)
    args = parser.parse_args()

    points = load_points(args.artifact, args.buy_confidence, args.sell_confidence)
    strategies = [
        simulate("first_close_raw", points, first_close_pairs, args),
        simulate("open_reprice_raw", points, open_reprice_pairs, args),
        simulate("open_reprice_directional_close_watch", points, directional_close_watch_pairs, args),
    ]

    output = {
        "source_artifact": str(args.artifact.resolve()),
        "quantity_per_action": args.quantity,
        "max_actions_per_symbol_day": MAX_ACTIONS_PER_SYMBOL_DAY,
        "max_pairs_per_symbol_day": MAX_PAIRS_PER_SYMBOL_DAY,
        "confidence_filter": {
            "buy_min_inclusive": args.buy_confidence,
            "sell_min_exclusive": args.sell_confidence,
        },
        "fee_model": {
            "commission_rate": args.commission_rate,
            "min_commission_per_order": args.min_commission,
            "stamp_duty_rate_sell_only": args.stamp_duty_rate,
            "transfer_fee_rate_both_sides": args.transfer_fee_rate,
        },
        "notes": [
            "Input must already be reviewed by AI; this script only accepts llm_status=ok and AI buy/sell decisions.",
            "This is offline accounting only: it does not import notification code, call Feishu, or start the backend.",
            "Each pair is one buy and one sell of one hand; a symbol can use at most 4 actions / 2 pairs per day.",
            "open_reprice variants replace a same-direction pending open with a better price before the opposite close.",
        ],
        "input_point_count_after_ai_filter": len(points),
        "strategies": strategies,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "strategies": summarize_for_stdout(strategies)}, ensure_ascii=False))


def load_points(path: Path, buy_confidence: float, sell_confidence: float) -> list[Point]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw_points: list[dict] = []
    if isinstance(raw.get("points"), list):
        raw_points = raw["points"]
    elif isinstance(raw.get("days"), list):
        for day in raw["days"]:
            for point in day.get("points", []):
                raw_points.append(point)
    else:
        raise SystemExit("Unsupported replay artifact: expected top-level points or days[].points")

    points = []
    for raw_point in raw_points:
        normalized = normalize_point(raw_point)
        if normalized is None:
            continue
        if normalized.action == "buy" and normalized.confidence >= buy_confidence:
            points.append(normalized)
        elif normalized.action == "sell" and normalized.confidence > sell_confidence:
            points.append(normalized)
    return sorted(points, key=lambda item: (item.symbol, item.date, item.timestamp))


def normalize_point(point: dict) -> Point | None:
    if point.get("llm_status") != "ok":
        return None
    if point.get("execution_allowed") is not True:
        return None
    action = point.get("llm_action")
    if action not in {"buy", "sell"}:
        return None
    confidence = point.get("llm_confidence")
    if confidence is None:
        return None
    timestamp = point.get("timestamp") or ""
    date_text = point.get("date") or timestamp[:10]
    time_text = point.get("time") or timestamp[11:16]
    if not date_text or not time_text:
        return None
    return Point(
        date=date_text,
        symbol=point["symbol"],
        timestamp=timestamp or f"{date_text}T{time_text}:00",
        time=time_text,
        action=action,
        price=float(point["price"]),
        confidence=float(confidence),
        rule_ids=list(point.get("rule_ids", [])),
        llm_summary=point.get("llm_summary"),
    )


def simulate(
    name: str,
    points: list[Point],
    pairer: Callable[[list[Point], argparse.Namespace], list[Pair]],
    args: argparse.Namespace,
) -> dict:
    grouped: dict[tuple[str, str], list[Point]] = defaultdict(list)
    for point in points:
        grouped[(point.symbol, point.date)].append(point)

    pairs: list[Pair] = []
    for key in sorted(grouped):
        pairs.extend(pairer(grouped[key], args))
    return summary(name, pairs)


def first_close_pairs(points: list[Point], args: argparse.Namespace) -> list[Pair]:
    pending_buy = None
    pending_sell = None
    pairs = []
    action_count = 0
    for point in sorted(points, key=lambda item: item.timestamp):
        if action_count >= MAX_ACTIONS_PER_SYMBOL_DAY or len(pairs) >= MAX_PAIRS_PER_SYMBOL_DAY:
            break
        if point.action == "buy":
            if pending_sell is not None:
                pairs.append(make_pair(pending_sell, point, args))
                pending_sell = None
                action_count += 1
            elif pending_buy is None:
                pending_buy = point
                action_count += 1
        elif point.action == "sell":
            if pending_buy is not None:
                pairs.append(make_pair(pending_buy, point, args))
                pending_buy = None
                action_count += 1
            elif pending_sell is None:
                pending_sell = point
                action_count += 1
    return pairs


def open_reprice_pairs(points: list[Point], args: argparse.Namespace) -> list[Pair]:
    pending_open = None
    pairs = []
    action_count = 0
    for point in sorted(points, key=lambda item: item.timestamp):
        if action_count >= MAX_ACTIONS_PER_SYMBOL_DAY or len(pairs) >= MAX_PAIRS_PER_SYMBOL_DAY:
            break
        if pending_open is None:
            pending_open = point
            action_count += 1
            continue
        if point.action == pending_open.action:
            if better_open(point, pending_open):
                pending_open = point
            continue
        pairs.append(make_pair(pending_open, point, args))
        pending_open = None
        action_count += 1
    return pairs


def directional_close_watch_pairs(points: list[Point], args: argparse.Namespace) -> list[Pair]:
    pending_open = None
    pairs = []
    action_count = 0
    for point in sorted(points, key=lambda item: item.timestamp):
        if action_count >= MAX_ACTIONS_PER_SYMBOL_DAY or len(pairs) >= MAX_PAIRS_PER_SYMBOL_DAY:
            break
        if pending_open is None:
            pending_open = point
            action_count += 1
            continue
        if point.action == pending_open.action:
            if better_open(point, pending_open):
                pending_open = point
            continue
        pair = make_pair(pending_open, point, args)
        if should_close_tracked_pair(pair):
            pairs.append(pair)
            pending_open = None
            action_count += 1
    return pairs


def make_pair(left: Point, right: Point, args: argparse.Namespace) -> Pair:
    if left.action == "buy":
        buy, sell, order = left, right, "buy->sell"
    else:
        buy, sell, order = right, left, "sell->buy"
    spread = round(sell.price - buy.price, 4)
    reference_price = buy.price if order == "buy->sell" else sell.price
    spread_pct = spread / reference_price * 100 if reference_price else 0
    gross_pnl = round(spread * args.quantity, 2)
    fees = trade_fees(buy.price, sell.price, args.quantity, args)
    return Pair(
        date=buy.date,
        symbol=buy.symbol,
        order=order,
        buy_time=buy.time,
        buy_price=buy.price,
        buy_confidence=buy.confidence,
        sell_time=sell.time,
        sell_price=sell.price,
        sell_confidence=sell.confidence,
        spread=spread,
        spread_pct=round(spread_pct, 4),
        gross_pnl=gross_pnl,
        fees=fees,
        net_pnl=round(gross_pnl - fees, 2),
    )


def trade_fees(buy_price: float, sell_price: float, quantity: int, args: argparse.Namespace) -> float:
    buy_amount = buy_price * quantity
    sell_amount = sell_price * quantity
    buy_commission = max(buy_amount * args.commission_rate, args.min_commission)
    sell_commission = max(sell_amount * args.commission_rate, args.min_commission)
    stamp_duty = sell_amount * args.stamp_duty_rate
    transfer_fee = (buy_amount + sell_amount) * args.transfer_fee_rate
    return round(buy_commission + sell_commission + stamp_duty + transfer_fee, 2)


def better_open(candidate: Point, current: Point) -> bool:
    if candidate.action == "buy":
        return candidate.price < current.price
    if candidate.action == "sell":
        return candidate.price > current.price
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


def summary(name: str, pairs: list[Pair]) -> dict:
    by_symbol = []
    grouped: dict[str, list[Pair]] = defaultdict(list)
    for pair in pairs:
        grouped[pair.symbol].append(pair)
    for symbol in sorted(grouped):
        by_symbol.append(aggregate(symbol, grouped[symbol]))
    return {
        "name": name,
        **aggregate("ALL", pairs),
        "by_symbol": by_symbol,
        "pairs": [asdict(pair) for pair in pairs],
    }


def aggregate(label: str, pairs: list[Pair]) -> dict:
    win_count = sum(1 for pair in pairs if pair.net_pnl > 0)
    return {
        "symbol": label,
        "paired_trade_count": len(pairs),
        "success_count": win_count,
        "success_rate_pct": round(win_count / len(pairs) * 100, 2) if pairs else 0,
        "total_spread": round(sum(pair.spread for pair in pairs), 4),
        "average_spread_per_pair": round(sum(pair.spread for pair in pairs) / len(pairs), 4) if pairs else 0,
        "total_fees": round(sum(pair.fees for pair in pairs), 2),
        "average_fee_per_pair": round(sum(pair.fees for pair in pairs) / len(pairs), 2) if pairs else 0,
        "total_pnl_gross": round(sum(pair.gross_pnl for pair in pairs), 2),
        "total_pnl_net": round(sum(pair.net_pnl for pair in pairs), 2),
        "average_pnl_gross_per_pair": round(sum(pair.gross_pnl for pair in pairs) / len(pairs), 2) if pairs else 0,
        "average_pnl_net_per_pair": round(sum(pair.net_pnl for pair in pairs) / len(pairs), 2) if pairs else 0,
    }


def summarize_for_stdout(strategies: list[dict]) -> list[dict]:
    keys = [
        "name",
        "paired_trade_count",
        "success_rate_pct",
        "average_spread_per_pair",
        "average_fee_per_pair",
        "total_pnl_gross",
        "total_pnl_net",
    ]
    return [{key: strategy[key] for key in keys} for strategy in strategies]


if __name__ == "__main__":
    main()
