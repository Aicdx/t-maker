from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from pydantic import BaseModel

from tmaker.domain.models import Candle, Position, ProviderHealth, Signal
from tmaker.domain.models import SignalAction
from tmaker.llm.context import build_review_context
from tmaker.llm.review import LlmReviewer, ReviewClient
from tmaker.market.bars import aggregate_five_minute
from tmaker.strategy.market_context import build_equal_weight_sector_candles, build_market_context
from tmaker.strategy.rules import MarketContext, evaluate_signal


class ReplayProvider(Protocol):
    def fetch_minutes(self, symbol: str) -> list[Candle]: ...


class ReplayPoint(BaseModel):
    symbol: str
    timestamp: str
    action: str
    kind: str
    price: float
    confidence: float
    rule_ids: list[str]
    reason: str
    risks: list[str]
    llm_status: str
    llm_action: str | None = None
    llm_confidence: float | None = None
    llm_summary: str | None = None
    llm_reasons: list[str] = []
    wait_for: list[str] = []
    execution_allowed: bool | None = None
    execution_blockers: list[str] = []


class ReplayResult(BaseModel):
    date: str
    mode: str
    strict: bool
    points: list[ReplayPoint]
    summary: dict[str, int]


class ReplayDayResult(BaseModel):
    date: str
    mode: str
    strict: bool
    chart_series: dict[str, list[Candle]]
    points: list[ReplayPoint]
    summary: dict[str, int | float | None]


class RecentReplayResult(BaseModel):
    schema_version: int = 2
    days_requested: int
    mode: str
    strict: bool
    review_enabled: bool
    symbols: list[str]
    days: list[ReplayDayResult]
    summary: dict[str, int | float | None]


@dataclass
class _ReplayCandidate:
    point: ReplayPoint
    signal: Signal
    candles: list[Candle]
    position: Position
    recent_signals: list[Signal]
    market_context: dict | None = None


@dataclass
class _ReplayTradeState:
    base_quantity: int
    t_quantity: int
    net_t_quantity: int = 0

    @property
    def needs_buy_restore(self) -> bool:
        return self.base_quantity > 0 and self.net_t_quantity < 0

    @property
    def needs_sell_restore(self) -> bool:
        return self.net_t_quantity > 0

    def record(self, action: str) -> None:
        quantity = self.t_quantity if self.t_quantity > 0 else 100
        if action == SignalAction.BUY.value:
            self.net_t_quantity += quantity
        elif action == SignalAction.SELL.value:
            self.net_t_quantity -= quantity


def replay_today(
    provider: ReplayProvider,
    symbols: Sequence[str],
    positions: Sequence[Position],
    review_client: ReviewClient | None = None,
    strict: bool = True,
) -> ReplayResult:
    all_signals: list[Signal] = []
    all_points: list[_ReplayCandidate] = []
    trade_date: date | None = None

    candles_by_symbol = {symbol: provider.fetch_minutes(symbol) for symbol in symbols}

    for symbol in symbols:
        candles = candles_by_symbol.get(symbol, [])
        if not candles:
            continue
        trade_date = candles[-1].timestamp.date()
        position = _position_for_symbol(positions, symbol)
        symbol_signals: list[Signal] = []

        for index in range(5, len(candles) + 1):
            window = candles[max(0, index - 30) : index]
            session_so_far = candles[:index]
            latest = window[-1]
            health = ProviderHealth(
                provider="replay",
                symbol=symbol,
                last_success_at=latest.timestamp,
                stale_after_seconds=600,
            )
            market_context = _market_context_for_symbol(symbol, session_so_far, candles_by_symbol)
            signal = evaluate_signal(
                window,
                [],
                position,
                health,
                now=latest.timestamp,
                session_candles=session_so_far,
                market_context=market_context,
            )
            if not signal.needs_llm_review:
                continue
            if any(existing.timestamp == signal.timestamp for existing in symbol_signals):
                continue

            symbol_signals.append(signal)
            all_signals.append(signal)
            all_points.append(
                _to_replay_candidate(
                    signal,
                    latest.close,
                    window,
                    position,
                    symbol_signals,
                    market_context.model_dump(mode="json") if market_context else None,
                )
            )

    compacted_candidates = _compact_points(all_points, strict=strict)
    if review_client is None:
        stateful_candidates = _stateful_replay_candidates(compacted_candidates, confirmed_only=True)
        compact_points = [candidate.point for candidate in stateful_candidates]
    else:
        compact_points = _review_points_statefully(compacted_candidates, review_client)
    return ReplayResult(
        date=trade_date.isoformat() if trade_date else "",
        mode=_replay_mode(strict),
        strict=strict,
        points=sorted(compact_points, key=lambda point: point.confidence, reverse=True),
        summary={
            "candidate_count": len(all_points),
            "buy_count": sum(1 for candidate in all_points if candidate.point.action == "buy"),
            "sell_count": sum(1 for candidate in all_points if candidate.point.action == "sell"),
            "reviewed_count": sum(1 for point in compact_points if point.llm_status == "ok"),
        },
    )


def replay_symbol_today(
    provider: ReplayProvider,
    symbol: str,
    positions: Sequence[Position],
    strict: bool = True,
    context_symbols: Sequence[str] | None = None,
) -> ReplayDayResult:
    candles_by_symbol = _provider_candles_for_symbols(
        provider,
        context_symbols or [position.symbol for position in positions],
    )
    candidates = _symbol_candidates(provider, symbol, positions, market_candles_by_symbol=candles_by_symbol)
    compacted = _stateful_replay_candidates(
        _compact_points(candidates, strict=strict),
        confirmed_only=True,
    )
    trade_date = candidates[-1].candles[-1].timestamp.date() if candidates else None
    candles = provider.fetch_minutes(symbol)
    return ReplayDayResult(
        date=trade_date.isoformat() if trade_date else candles[-1].timestamp.date().isoformat() if candles else "",
        mode=_replay_mode(strict),
        strict=strict,
        chart_series=_chart_series({symbol: candles}),
        points=[candidate.point for candidate in compacted],
        summary={
            "candidate_count": len(candidates),
            "buy_count": sum(1 for candidate in candidates if candidate.point.action == "buy"),
            "sell_count": sum(1 for candidate in candidates if candidate.point.action == "sell"),
            "reviewed_count": 0,
        },
    )


def review_symbol_point(
    provider: ReplayProvider,
    symbol: str,
    timestamp: str,
    positions: Sequence[Position],
    review_client: ReviewClient,
    strict: bool = True,
    context_symbols: Sequence[str] | None = None,
) -> ReplayPoint | None:
    candles_by_symbol = _provider_candles_for_symbols(
        provider,
        context_symbols or [position.symbol for position in positions],
    )
    candidates = _stateful_replay_candidates(
        _compact_points(
            _symbol_candidates(provider, symbol, positions, market_candles_by_symbol=candles_by_symbol),
            strict=strict,
        ),
        confirmed_only=True,
    )
    for candidate in candidates:
        if candidate.point.timestamp == timestamp:
            return _review_points([candidate], review_client)[0]
    return None


def replay_recent_days(
    provider: ReplayProvider,
    symbols: Sequence[str],
    positions: Sequence[Position],
    review_client: ReviewClient | None = None,
    days: int = 5,
    strict: bool = True,
) -> RecentReplayResult:
    candles_by_symbol = {symbol: provider.fetch_minutes(symbol) for symbol in symbols}
    trade_dates = _latest_trade_dates(candles_by_symbol, days)
    day_results: list[ReplayDayResult] = []

    for trade_date in trade_dates:
        day_candles_by_symbol = {
            symbol: [candle for candle in candles if candle.timestamp.date() == trade_date]
            for symbol, candles in candles_by_symbol.items()
        }
        day_provider = _StaticReplayProvider(day_candles_by_symbol)
        result = replay_today(day_provider, symbols, positions, review_client, strict=strict)
        day_results.append(
            ReplayDayResult(
                date=trade_date.isoformat(),
                mode=result.mode,
                strict=result.strict,
                chart_series=_chart_series(day_candles_by_symbol),
                points=result.points,
                summary=_with_accuracy_summary(result.summary, result.points, day_candles_by_symbol),
            )
        )

    return RecentReplayResult(
        days_requested=days,
        mode=_replay_mode(strict),
        strict=strict,
        review_enabled=review_client is not None,
        symbols=list(symbols),
        days=day_results,
        summary=_combine_recent_summary(day_results),
    )


def _symbol_candidates(
    provider: ReplayProvider,
    symbol: str,
    positions: Sequence[Position],
    market_candles_by_symbol: dict[str, list[Candle]] | None = None,
) -> list[_ReplayCandidate]:
    candles = provider.fetch_minutes(symbol)
    if not candles:
        return []

    position = _position_for_symbol(positions, symbol)
    symbol_signals: list[Signal] = []
    candidates: list[_ReplayCandidate] = []

    for index in range(5, len(candles) + 1):
        window = candles[max(0, index - 30) : index]
        session_so_far = candles[:index]
        latest = window[-1]
        health = ProviderHealth(
            provider="replay",
            symbol=symbol,
            last_success_at=latest.timestamp,
            stale_after_seconds=600,
        )
        market_context = _market_context_for_symbol(symbol, session_so_far, market_candles_by_symbol)
        signal = evaluate_signal(
            window,
            [],
            position,
            health,
            now=latest.timestamp,
            session_candles=session_so_far,
            market_context=market_context,
        )
        if not signal.needs_llm_review:
            continue
        if any(existing.timestamp == signal.timestamp for existing in symbol_signals):
            continue

        symbol_signals.append(signal)
        candidates.append(
            _to_replay_candidate(
                signal,
                latest.close,
                window,
                position,
                symbol_signals,
                market_context.model_dump(mode="json") if market_context else None,
            )
        )

    return candidates


def _provider_candles_for_symbols(
    provider: ReplayProvider,
    symbols: Sequence[str],
) -> dict[str, list[Candle]]:
    candles_by_symbol: dict[str, list[Candle]] = {}
    for symbol in symbols:
        try:
            candles = provider.fetch_minutes(symbol)
        except Exception:
            continue
        if candles:
            candles_by_symbol[symbol] = candles
    return candles_by_symbol


def _market_context_for_symbol(
    symbol: str,
    session_so_far: list[Candle],
    candles_by_symbol: dict[str, list[Candle]] | None,
) -> MarketContext | None:
    if not candles_by_symbol:
        return None
    latest_time = session_so_far[-1].timestamp if session_so_far else None
    if latest_time is None:
        return None
    truncated = {
        item_symbol: [candle for candle in candles if candle.timestamp <= latest_time]
        for item_symbol, candles in candles_by_symbol.items()
    }
    index_symbol = _index_symbol_for_context(truncated)
    index_candles = truncated.get(index_symbol, []) if index_symbol else []
    sector_source = {
        item_symbol: candles
        for item_symbol, candles in truncated.items()
        if item_symbol not in _INDEX_SYMBOLS
    }
    sector_candles = build_equal_weight_sector_candles(symbol, sector_source)
    if not sector_candles:
        return None
    context = build_market_context(
        session_so_far,
        index_candles=index_candles,
        sector_candles=sector_candles,
    )
    return context


_INDEX_SYMBOLS = {"399006", "000001", "000300"}


def _index_symbol_for_context(candles_by_symbol: dict[str, list[Candle]]) -> str | None:
    for symbol in ("399006", "000300", "000001"):
        if symbol in candles_by_symbol and candles_by_symbol[symbol]:
            return symbol
    return None


def _stateful_replay_candidates(
    candidates: list[_ReplayCandidate],
    *,
    confirmed_only: bool,
) -> list[_ReplayCandidate]:
    states: dict[str, _ReplayTradeState] = {}
    recent_by_symbol: dict[str, list[Signal]] = defaultdict(list)
    stateful: list[_ReplayCandidate] = []

    for candidate in sorted(candidates, key=lambda item: (item.point.symbol, item.point.timestamp)):
        state = states.setdefault(
            candidate.point.symbol,
            _ReplayTradeState(candidate.position.base_quantity, candidate.position.t_quantity),
        )
        signal = candidate.signal
        if _same_direction_while_unbalanced(signal, state):
            continue

        signal = _apply_trade_state(signal, state)
        recent = [*recent_by_symbol[candidate.point.symbol], signal][-5:]
        stateful_candidate = replace(
            candidate,
            signal=signal,
            point=_to_replay_point(signal, candidate.point.price),
            recent_signals=recent,
        )
        stateful.append(stateful_candidate)
        recent_by_symbol[candidate.point.symbol].append(signal)
        if not confirmed_only:
            state.record(signal.action.value)

    return stateful


def _review_points_statefully(
    candidates: list[_ReplayCandidate],
    review_client: ReviewClient,
) -> list[ReplayPoint]:
    reviewer = LlmReviewer(review_client)
    states: dict[str, _ReplayTradeState] = {}
    recent_by_symbol: dict[str, list[Signal]] = defaultdict(list)
    reviewed_points: list[ReplayPoint] = []

    for candidate in sorted(candidates, key=lambda item: (item.point.symbol, item.point.timestamp)):
        state = states.setdefault(
            candidate.point.symbol,
            _ReplayTradeState(candidate.position.base_quantity, candidate.position.t_quantity),
        )
        signal = candidate.signal
        signal = _apply_trade_state(signal, state)
        recent = [*recent_by_symbol[candidate.point.symbol], signal][-5:]
        reviewed = asyncio.run(
            reviewer.review(
                signal,
                build_review_context(
                    signal,
                    candidate.candles,
                    candidate.position,
                    recent,
                    market_context=candidate.market_context,
                ),
            )
        )
        point = _to_replay_point(reviewed, candidate.point.price)
        reviewed_points.append(point)
        recent_by_symbol[candidate.point.symbol].append(reviewed)
        if point.llm_action in {SignalAction.BUY.value, SignalAction.SELL.value}:
            state.record(point.llm_action)

    return reviewed_points


def _same_direction_while_unbalanced(signal: Signal, trade_state: _ReplayTradeState) -> bool:
    return (
        signal.action == SignalAction.BUY
        and trade_state.needs_sell_restore
        or signal.action == SignalAction.SELL
        and trade_state.needs_buy_restore
    )


def _apply_trade_state(signal: Signal, trade_state: _ReplayTradeState) -> Signal:
    if signal.action == SignalAction.BUY and trade_state.needs_buy_restore:
        return _with_trade_state_bias(
            signal,
            rule_id="restore_after_intraday_sell",
            reason="已高抛导致日内持仓低于底仓，后续低吸应优先用于回补底仓",
            confidence_boost=0.08,
        )
    if signal.action == SignalAction.SELL and trade_state.needs_sell_restore:
        return _with_trade_state_bias(
            signal,
            rule_id="restore_after_intraday_buy",
            reason="已低吸导致日内持仓高于底仓，后续高抛应优先用于恢复底仓",
            confidence_boost=0.08,
        )
    return signal


def _with_trade_state_bias(
    signal: Signal,
    *,
    rule_id: str,
    reason: str,
    confidence_boost: float,
) -> Signal:
    rule_ids = signal.rule_ids if rule_id in signal.rule_ids else [*signal.rule_ids, rule_id]
    return signal.model_copy(
        update={
            "confidence": min(signal.confidence + confidence_boost, 0.95),
            "rule_ids": rule_ids,
            "reason": f"{signal.reason}；{reason}",
        }
    )


def _to_replay_candidate(
    signal: Signal,
    price: float,
    candles: list[Candle],
    position: Position,
    recent_signals: list[Signal],
    market_context: dict | None = None,
) -> _ReplayCandidate:
    return _ReplayCandidate(
        point=_to_replay_point(signal, price),
        signal=signal,
        candles=candles,
        position=position,
        recent_signals=recent_signals,
        market_context=market_context,
    )


def _to_replay_point(signal: Signal, price: float) -> ReplayPoint:
    review = signal.llm_review
    confidence = review.confidence if review else signal.confidence
    return ReplayPoint(
        symbol=signal.symbol,
        timestamp=signal.timestamp.isoformat(),
        action=signal.action.value,
        kind=signal.kind.value,
        price=price,
        confidence=confidence,
        rule_ids=signal.rule_ids,
        reason=signal.reason,
        risks=signal.risks + (review.risks if review else []),
        llm_status=signal.llm_status,
        llm_action=review.action.value if review else None,
        llm_confidence=review.confidence if review else None,
        llm_summary=review.summary if review else None,
        llm_reasons=review.reasons if review else [],
        wait_for=review.wait_for if review else [],
        execution_allowed=review.execution_allowed if review else None,
        execution_blockers=review.execution_blockers if review else [],
    )


def _review_points(
    candidates: list[_ReplayCandidate],
    review_client: ReviewClient | None,
) -> list[ReplayPoint]:
    if review_client is None:
        return [candidate.point for candidate in candidates]
    reviewed_points: list[ReplayPoint] = []
    reviewer = LlmReviewer(review_client)

    for candidate in candidates:
        context = build_review_context(
            candidate.signal,
            candidate.candles,
            candidate.position,
            candidate.recent_signals,
            market_context=candidate.market_context,
        )
        reviewed = asyncio.run(reviewer.review(candidate.signal, context))
        reviewed_points.append(_to_replay_point(reviewed, candidate.point.price))

    return reviewed_points


def should_keep_realtime_candidate(
    previous_signals: Sequence[tuple[Signal, float]],
    signal: Signal,
    price: float,
    position: Position,
) -> bool:
    if not signal.needs_llm_review:
        return True

    candidates = [
        _to_replay_candidate(existing_signal, existing_price, [], position, [])
        for existing_signal, existing_price in previous_signals
        if existing_signal.symbol == signal.symbol
        and existing_signal.needs_llm_review
        and existing_signal.timestamp <= signal.timestamp
    ]
    candidate = _to_replay_candidate(signal, price, [], position, [])
    compacted = _compact_points([*candidates, candidate], strict=True)
    return any(item.signal is signal for item in compacted)


def _compact_points(candidates: list[_ReplayCandidate], strict: bool = True) -> list[_ReplayCandidate]:
    compacted: list[_ReplayCandidate] = []
    current: list[_ReplayCandidate] = []

    for candidate in sorted(candidates, key=lambda item: (item.point.symbol, item.point.timestamp)):
        if current and (
            candidate.point.symbol != current[-1].point.symbol
            or candidate.point.action != current[-1].point.action
            or _compact_rule_key(candidate.point.rule_ids) != _compact_rule_key(current[-1].point.rule_ids)
            or not _is_nearby_minute(current[-1].point, candidate.point, current)
            or _starts_new_sell_high_leg(current, candidate)
            or _starts_new_buy_low_leg(current, candidate)
        ):
            compacted.append(_representative_point(current, strict))
            current = []
        current.append(candidate)

    if current:
        compacted.append(_representative_point(current, strict))

    return compacted


def _representative_point(candidates: list[_ReplayCandidate], strict: bool) -> _ReplayCandidate:
    if strict:
        return candidates[0]
    if _is_lunch_duplicate_sell_cluster(candidates):
        return candidates[0]
    if _is_intraday_gain_sell_cluster(candidates):
        prelunch_peak = _prelunch_peak_if_lunch_continuation(candidates)
        if prelunch_peak is not None:
            return prelunch_peak
        return max(candidates, key=lambda candidate: (candidate.point.price, candidate.point.timestamp))
    return _best_point(candidates)


def _is_intraday_gain_sell_cluster(candidates: list[_ReplayCandidate]) -> bool:
    return bool(candidates) and candidates[0].point.action == "sell" and any(
        "intraday_gain_session_vwap_stretch" in candidate.point.rule_ids for candidate in candidates
    )


def _is_sell_observation_cluster(candidates: list[_ReplayCandidate]) -> bool:
    if not candidates or candidates[0].point.action != "sell":
        return False
    observation_rules = {
        "intraday_gain_session_vwap_stretch",
        "suspected_vwap_high_stretch",
    }
    return any(
        any(rule_id in observation_rules for rule_id in candidate.point.rule_ids)
        for candidate in candidates
    )


def _is_lunch_duplicate_sell_cluster(candidates: list[_ReplayCandidate]) -> bool:
    if len(candidates) < 2 or candidates[0].point.action != "sell":
        return False
    first = candidates[0].point
    last = candidates[-1].point
    return (
        first.timestamp[11:16] <= "11:30"
        and last.timestamp[11:16] >= "13:00"
        and _compact_rule_key(first.rule_ids) == _compact_rule_key(last.rule_ids)
        and abs(first.price - last.price) <= max(first.price * 0.001, 0.01)
    )


def _starts_new_sell_high_leg(
    cluster: list[_ReplayCandidate],
    candidate: _ReplayCandidate,
) -> bool:
    if not cluster or candidate.point.action != "sell" or not _is_sell_observation_cluster(cluster):
        return False
    from datetime import datetime

    cluster_start = datetime.fromisoformat(cluster[0].point.timestamp)
    candidate_time = datetime.fromisoformat(candidate.point.timestamp)
    elapsed_seconds = (candidate_time - cluster_start).total_seconds()
    crossed_lunch = cluster[0].point.timestamp[11:16] <= "11:30" and candidate.point.timestamp[11:16] >= "13:00"
    if elapsed_seconds < 10 * 60 and not crossed_lunch:
        return False
    cluster_high = max(item.point.price for item in cluster)
    return candidate.point.price >= cluster_high * 1.002


def _prelunch_peak_if_lunch_continuation(
    candidates: list[_ReplayCandidate],
) -> _ReplayCandidate | None:
    if not candidates or candidates[0].point.action != "sell":
        return None
    prelunch = [candidate for candidate in candidates if candidate.point.timestamp[11:16] <= "11:30"]
    postlunch = [candidate for candidate in candidates if candidate.point.timestamp[11:16] >= "13:00"]
    if not prelunch or not postlunch:
        return None
    pre_peak = max(prelunch, key=lambda candidate: (candidate.point.price, candidate.point.timestamp))
    post_first = postlunch[0]
    if abs(pre_peak.point.price - post_first.point.price) <= max(pre_peak.point.price * 0.001, 0.01):
        return pre_peak
    return None


def _best_point(candidates: list[_ReplayCandidate]) -> _ReplayCandidate:
    if candidates[0].point.action == "buy":
        return min(candidates, key=lambda candidate: candidate.point.price)
    if candidates[0].point.action == "sell":
        return max(candidates, key=lambda candidate: candidate.point.price)
    return max(candidates, key=lambda candidate: candidate.point.confidence)


def _is_pullback_buy_cluster(candidates: list[_ReplayCandidate]) -> bool:
    return bool(candidates) and candidates[0].point.action == "buy" and any(
        "pullback_low_rebound" in candidate.point.rule_ids for candidate in candidates
    )


def _compact_rule_key(rule_ids: list[str]) -> list[str]:
    modifiers = {
        "market_sector_uptrend_sell_downgrade",
        "market_context_sell_boost",
    }
    core = [rule_id for rule_id in rule_ids if rule_id not in modifiers]
    if any(
        rule_id in {"intraday_gain_session_vwap_stretch", "suspected_vwap_high_stretch"}
        for rule_id in core
    ):
        return ["sell_high_stretch"]
    return core


def _starts_new_buy_low_leg(
    cluster: list[_ReplayCandidate],
    candidate: _ReplayCandidate,
) -> bool:
    if not cluster or candidate.point.action != "buy" or not _is_pullback_buy_cluster(cluster):
        return False
    cluster_low = min(item.point.price for item in cluster)
    return candidate.point.price <= cluster_low * 0.995


def _is_nearby_minute(
    previous: ReplayPoint,
    current: ReplayPoint,
    cluster: list[_ReplayCandidate] | None = None,
) -> bool:
    from datetime import datetime

    previous_time = datetime.fromisoformat(previous.timestamp)
    current_time = datetime.fromisoformat(current.timestamp)
    if previous_time.time().strftime("%H:%M") <= "11:30" and current_time.time().strftime("%H:%M") >= "13:00":
        return bool(
            previous.action == current.action == "sell"
            and _compact_rule_key(previous.rule_ids) == _compact_rule_key(current.rule_ids)
            and abs(previous.price - current.price) <= max(previous.price * 0.001, 0.01)
        )
    max_gap_seconds = (
        15 * 60
        if cluster and (_is_sell_observation_cluster(cluster) or _is_pullback_buy_cluster(cluster))
        else 120
    )
    return (current_time - previous_time).total_seconds() <= max_gap_seconds


def _position_for_symbol(positions: Sequence[Position], symbol: str) -> Position:
    for position in positions:
        if position.symbol == symbol:
            return position
    return Position(symbol=symbol, base_quantity=0, cost_price=0, available_cash=0, t_quantity=0)


class _StaticReplayProvider:
    def __init__(self, candles_by_symbol: dict[str, list[Candle]]) -> None:
        self.candles_by_symbol = candles_by_symbol

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        return self.candles_by_symbol.get(symbol, [])


def _latest_trade_dates(candles_by_symbol: dict[str, list[Candle]], days: int) -> list[date]:
    trade_dates = sorted(
        {
            candle.timestamp.date()
            for candles in candles_by_symbol.values()
            for candle in candles
        }
    )
    return trade_dates[-days:]


def _chart_series(candles_by_symbol: dict[str, list[Candle]]) -> dict[str, list[Candle]]:
    one_minute = [
        candle
        for candles in candles_by_symbol.values()
        for candle in sorted(candles, key=lambda item: item.timestamp)
    ]
    return {
        "realtime": one_minute,
        "one_minute": one_minute,
        "five_minute": [
            candle
            for candles in candles_by_symbol.values()
            for candle in aggregate_five_minute(candles)
        ],
    }


def _with_accuracy_summary(
    base_summary: dict[str, int],
    points: list[ReplayPoint],
    candles_by_symbol: dict[str, list[Candle]],
) -> dict[str, int | float | None]:
    checked = [_point_accuracy(point, candles_by_symbol.get(point.symbol, [])) for point in points]
    checked = [item for item in checked if item is not None]
    hit_count = sum(1 for item in checked if item)
    total = len(checked)
    return {
        **base_summary,
        "ai_buy_count": sum(1 for point in points if point.llm_action == "buy"),
        "ai_sell_count": sum(1 for point in points if point.llm_action == "sell"),
        "ai_hold_count": sum(1 for point in points if point.llm_action == "hold"),
        "accuracy_checked_count": total,
        "accuracy_hit_count": hit_count,
        "accuracy_rate_pct": round(hit_count / total * 100, 2) if total else None,
    }


def _combine_recent_summary(days: list[ReplayDayResult]) -> dict[str, int | float | None]:
    totals: dict[str, int] = defaultdict(int)
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
        "accuracy_checked_count": checked,
        "accuracy_hit_count": hit_count,
        "accuracy_rate_pct": round(hit_count / checked * 100, 2) if checked else None,
    }


def _point_accuracy(point: ReplayPoint, candles: list[Candle]) -> bool | None:
    action = point.llm_action or point.action
    if action not in {"buy", "sell"}:
        return None

    timestamp = _parse_timestamp(point.timestamp)
    future = [
        candle
        for candle in sorted(candles, key=lambda item: item.timestamp)
        if 0 < (candle.timestamp - timestamp).total_seconds() <= 30 * 60
    ]
    if not future:
        return None

    threshold_pct = 0.6
    if action == "buy":
        best = max(candle.high for candle in future)
        return (best - point.price) / point.price * 100 >= threshold_pct

    best = min(candle.low for candle in future)
    return (point.price - best) / point.price * 100 >= threshold_pct


def _parse_timestamp(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


def _replay_mode(strict: bool) -> str:
    return "strict" if strict else "optimized"
