from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from tmaker.storage.postgres import PostgresRepository  # noqa: E402
from tmaker.strategy.indicators import compute_indicators  # noqa: E402

ARTIFACT = ROOT / "artifacts" / "ai-replay-300502-300308-2026-05-08_2026-06-12.json"
OUTPUT = ROOT / "artifacts" / "combined-strategy-lab-summary.json"
QUANTITY = 100
MAX_ACTIONS_PER_SYMBOL_DAY = 4
MAX_PAIRS_PER_SYMBOL_DAY = 2


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

    @property
    def pnl(self) -> float:
        return round(self.spread * QUANTITY, 2)


class Lab:
    def __init__(self) -> None:
        self.root = ROOT
        self.points = json.loads(ARTIFACT.read_text(encoding="utf-8"))["points"]
        self.repository = PostgresRepository(self._database_url())
        self._candles_cache = {}

    def run(self) -> dict:
        strategies = [
            self._simulate("original_first_close_raw", self._keep_all, self._first_close_pairs),
            self._simulate("stock_specific_first_close_raw", self._stock_specific_filter, self._first_close_pairs),
            self._simulate("stock_specific_open_reprice_raw", self._stock_specific_filter, self._open_reprice_pairs),
            self._simulate(
                "stock_specific_open_reprice_directional_close_watch",
                self._stock_specific_filter,
                self._open_reprice_directional_close_watch_pairs,
            ),
            self._simulate("stock_specific_best_pair_reference", self._stock_specific_filter, self._best_pair_reference),
        ]
        for threshold in (0.0, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0):
            strategies.append(
                self._simulate(
                    f"stock_specific_close_floor_{threshold:.1f}pct",
                    self._stock_specific_filter,
                    lambda points, floor=threshold: self._first_close_pairs(points, min_spread_pct=floor),
                )
            )
            strategies.append(
                self._simulate(
                    f"stock_specific_open_reprice_close_floor_{threshold:.1f}pct",
                    self._stock_specific_filter,
                    lambda points, floor=threshold: self._open_reprice_pairs(points, min_spread_pct=floor),
                )
            )

        result = {
            "source_artifact": str(ARTIFACT),
            "quantity_per_action": QUANTITY,
            "max_actions_per_symbol_day": MAX_ACTIONS_PER_SYMBOL_DAY,
            "max_pairs_per_symbol_day": MAX_PAIRS_PER_SYMBOL_DAY,
            "notes": [
                "This lab is read-only and does not call Feishu or the live monitor.",
                "best_pair_reference uses same-day lookahead and is a potential ceiling, not directly live-safe.",
                "close_floor variants only close an opened T pair when the observed opposite signal reaches the spread floor.",
                "open_reprice variants treat same-direction signals before the opposite side as a watchlist and keep the better opening price.",
                "directional_close_watch waits longer on buy->sell profit taking, but restores after sell->buy once about 1% spread is available.",
            ],
            "strategies": strategies,
        }
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _simulate(
        self,
        name: str,
        keep_point: Callable[[dict], bool],
        pairer: Callable[[list[dict]], list[Pair]],
    ) -> dict:
        grouped = defaultdict(list)
        blocked = []
        for point in self.points:
            if not self._eligible(point):
                continue
            if keep_point(point):
                grouped[(point["symbol"], point["date"])].append(point)
            else:
                blocked.append(point)

        pairs = []
        for key in sorted(grouped):
            pairs.extend(pairer(grouped[key]))

        return self._summary(name, pairs, blocked)

    def _summary(self, name: str, pairs: list[Pair], blocked: list[dict]) -> dict:
        by_symbol = []
        pairs_by_symbol = defaultdict(list)
        for pair in pairs:
            pairs_by_symbol[pair.symbol].append(pair)
        for symbol in sorted(pairs_by_symbol):
            symbol_pairs = pairs_by_symbol[symbol]
            by_symbol.append(self._aggregate(symbol, symbol_pairs))

        return {
            "name": name,
            **self._aggregate("ALL", pairs),
            "by_symbol": by_symbol,
            "blocked_eligible_count": len(blocked),
            "blocked_eligible_points": [
                {
                    "symbol": point["symbol"],
                    "date": point["date"],
                    "time": point["time"],
                    "llm_action": point["llm_action"],
                    "price": point["price"],
                    "llm_confidence": point["llm_confidence"],
                    "rule_ids": point["rule_ids"],
                }
                for point in blocked
            ],
            "pairs": [pair.__dict__ | {"pnl": pair.pnl} for pair in pairs],
        }

    def _aggregate(self, label: str, pairs: list[Pair]) -> dict:
        success_count = sum(1 for pair in pairs if pair.spread > 0)
        total_spread = sum(pair.spread for pair in pairs)
        total_pnl = sum(pair.pnl for pair in pairs)
        return {
            "symbol": label,
            "paired_trade_count": len(pairs),
            "success_count": success_count,
            "success_rate_pct": round(success_count / len(pairs) * 100, 2) if pairs else 0,
            "total_pnl_gross": round(total_pnl, 2),
            "average_spread_per_pair": round(total_spread / len(pairs), 4) if pairs else 0,
            "average_pnl_per_pair": round(total_pnl / len(pairs), 2) if pairs else 0,
        }

    def _first_close_pairs(self, points: list[dict], min_spread_pct: float | None = None) -> list[Pair]:
        pending_buy = None
        pending_sell = None
        pairs = []
        action_count = 0

        for point in sorted(points, key=lambda item: item["timestamp"]):
            if action_count >= MAX_ACTIONS_PER_SYMBOL_DAY or len(pairs) >= MAX_PAIRS_PER_SYMBOL_DAY:
                break
            action = point["llm_action"]
            if action == "buy":
                if pending_sell is not None:
                    pair = self._make_pair(pending_sell, point)
                    if min_spread_pct is None or pair.spread_pct >= min_spread_pct:
                        pairs.append(pair)
                        pending_sell = None
                        action_count += 1
                elif pending_buy is None:
                    pending_buy = point
                    action_count += 1
            elif action == "sell":
                if pending_buy is not None:
                    pair = self._make_pair(pending_buy, point)
                    if min_spread_pct is None or pair.spread_pct >= min_spread_pct:
                        pairs.append(pair)
                        pending_buy = None
                        action_count += 1
                elif pending_sell is None:
                    pending_sell = point
                    action_count += 1
        return pairs

    def _open_reprice_directional_close_watch_pairs(self, points: list[dict]) -> list[Pair]:
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
                if self._better_open(point, pending_open):
                    pending_open = point
                continue

            pair = self._make_pair(pending_open, point)
            if self._should_close_tracked_pair(pair):
                pairs.append(pair)
                pending_open = None
                action_count += 1

        return pairs

    def _open_reprice_pairs(self, points: list[dict], min_spread_pct: float | None = None) -> list[Pair]:
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
                if self._better_open(point, pending_open):
                    pending_open = point
                continue

            pair = self._make_pair(pending_open, point)
            if min_spread_pct is None or pair.spread_pct >= min_spread_pct:
                pairs.append(pair)
                pending_open = None
                action_count += 1

        return pairs

    def _best_pair_reference(self, points: list[dict]) -> list[Pair]:
        sorted_points = sorted(points, key=lambda item: item["timestamp"])
        candidates = []
        for left_index, left in enumerate(sorted_points):
            for right in sorted_points[left_index + 1 :]:
                if left["llm_action"] == right["llm_action"]:
                    continue
                candidates.append(self._make_pair(left, right))

        selected = []
        used_timestamps = set()
        for pair in sorted(candidates, key=lambda item: item.spread, reverse=True):
            timestamps = {pair.buy_time, pair.sell_time}
            if timestamps & used_timestamps:
                continue
            selected.append(pair)
            used_timestamps.update(timestamps)
            if len(selected) >= MAX_PAIRS_PER_SYMBOL_DAY:
                break

        return sorted(selected, key=lambda item: (item.date, item.symbol, min(item.buy_time, item.sell_time)))

    def _make_pair(self, left: dict, right: dict) -> Pair:
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
        spread_pct = spread / reference_price * 100 if reference_price else 0
        return Pair(
            date=buy["date"],
            symbol=buy["symbol"],
            order=order,
            buy_time=buy["time"],
            buy_price=buy["price"],
            buy_confidence=buy["llm_confidence"],
            sell_time=sell["time"],
            sell_price=sell["price"],
            sell_confidence=sell["llm_confidence"],
            spread=spread,
            spread_pct=round(spread_pct, 4),
        )

    def _better_open(self, candidate: dict, current: dict) -> bool:
        if candidate["llm_action"] == "buy":
            return candidate["price"] < current["price"]
        if candidate["llm_action"] == "sell":
            return candidate["price"] > current["price"]
        return False

    def _should_close_tracked_pair(self, pair: Pair) -> bool:
        close_time = pair.sell_time if pair.order == "buy->sell" else pair.buy_time
        if pair.order == "sell->buy":
            return pair.spread_pct >= 1.0 or (close_time >= "14:30" and pair.spread > 0)
        return (
            pair.spread_pct >= 3.0
            or (close_time >= "13:00" and pair.spread_pct >= 1.0)
            or (close_time >= "14:30" and pair.spread > 0)
        )

    def _stock_specific_filter(self, point: dict) -> bool:
        if point["symbol"] != "300502":
            return True
        return not self._neway_weak_buy_guard(point)

    def _neway_weak_buy_guard(self, point: dict) -> bool:
        if not self._is_low_buy_rule(point):
            return False
        metrics = self._metrics(point)
        if metrics is None:
            return False
        extreme_capitulation_rebound = (
            metrics["fade_from_high_pct"] <= -10.0
            and metrics["session_vwap_dev_pct"] <= -3.0
            and metrics["intraday_change_pct"] <= -6.0
            and metrics["near_session_low_pct"] <= 1.0
            and metrics["rebound_pct"] >= 0.6
            and metrics["volume_down"]
        )
        return (
            metrics["fade_from_high_pct"] <= -6.0
            and metrics["session_vwap_dev_pct"] <= -3.0
            and metrics["intraday_change_pct"] <= -3.0
            and not extreme_capitulation_rebound
        ) or (
            metrics["intraday_change_pct"] <= -2.5
            and metrics["session_vwap_dev_pct"] > -1.45
        )

    def _metrics(self, point: dict) -> dict | None:
        candles = self._candles(point["symbol"], point["date"])
        timestamp = datetime.fromisoformat(point["timestamp"])
        index = next((idx for idx, candle in enumerate(candles) if candle.timestamp == timestamp), None)
        if index is None:
            return None
        session = candles[: index + 1]
        if len(session) < 2:
            return None
        window = session[-30:]
        latest = session[-1]
        previous = session[-2]
        indicators = compute_indicators(window, [], session)
        high_so_far = max(candle.high for candle in session)
        session_low = min(candle.low for candle in session)
        return {
            "intraday_change_pct": indicators.intraday_change_pct,
            "session_vwap_dev_pct": indicators.price_session_vwap_deviation_pct,
            "fade_from_high_pct": ((latest.close - high_so_far) / high_so_far * 100) if high_so_far else 0,
            "near_session_low_pct": ((latest.close - session_low) / session_low * 100) if session_low else 0,
            "rebound_pct": ((latest.close - previous.close) / previous.close * 100) if previous.close else 0,
            "volume_down": latest.volume <= previous.volume,
        }

    def _candles(self, symbol: str, date_text: str):
        key = (symbol, date_text)
        if key not in self._candles_cache:
            year, month, day = (int(part) for part in date_text.split("-"))
            self._candles_cache[key] = self.repository.get_minute_bars(symbol, date(year, month, day))
        return self._candles_cache[key]

    def _eligible(self, point: dict) -> bool:
        if point.get("llm_status") != "ok" or point.get("execution_allowed") is not True:
            return False
        action = point.get("llm_action")
        confidence = point.get("llm_confidence") or 0
        return (action == "sell" and confidence > 0.58) or (action == "buy" and confidence >= 0.54)

    def _keep_all(self, point: dict) -> bool:
        return True

    def _is_low_buy_rule(self, point: dict) -> bool:
        return point.get("rule_action") == "buy" and any(
            rule_id in {"pullback_low_rebound", "deep_session_vwap_low_buy"}
            for rule_id in point.get("rule_ids", [])
        )

    def _database_url(self) -> str:
        env_file = self.root / "backend" / ".env"
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1]
        return os.environ["DATABASE_URL"]


if __name__ == "__main__":
    summary = Lab().run()
    for strategy in summary["strategies"]:
        print(
            strategy["name"],
            "pairs=",
            strategy["paired_trade_count"],
            "wins=",
            strategy["success_count"],
            "pnl=",
            strategy["total_pnl_gross"],
            "avg=",
            strategy["average_spread_per_pair"],
        )
    print(f"Wrote {OUTPUT}")
