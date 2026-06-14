from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from tmaker.domain.models import (
    Candle,
    Position,
    ProviderHealth,
    Signal,
    SignalAction,
    SignalKind,
)
from tmaker.strategy.indicators import IndicatorSnapshot, compute_indicators


class RuleThresholds(BaseModel):
    sharp_drop_pct: float = -7.0
    vwap_low_deviation_pct: float = -2.5
    vwap_high_deviation_pct: float = 3.0
    intraday_gain_sell_pct: float = 4.5
    session_vwap_high_deviation_pct: float = 1.5
    relative_weakness_pct: float = -0.8
    suspected_drop_pct: float = -4.0
    suspected_vwap_low_deviation_pct: float = -1.4
    suspected_vwap_high_deviation_pct: float = 1.8
    suspected_local_vwap_low_deviation_pct: float = -1.8
    suspected_local_vwap_high_deviation_pct: float = 1.6
    suspected_intraday_gain_sell_pct: float = 4.0
    suspected_session_vwap_high_deviation_pct: float = 1.0
    pullback_session_vwap_low_deviation_pct: float = -1.2
    pullback_near_recent_low_pct: float = 0.8
    pullback_rebound_pct: float = 0.15
    deep_session_vwap_low_deviation_pct: float = -2.0
    deep_session_near_low_pct: float = 0.8
    weak_rebound_vwap_fail_max_intraday_pct: float = -1.0
    weak_rebound_min_rebound_from_low_pct: float = 1.0
    weak_rebound_max_price_session_vwap_deviation_pct: float = -0.1
    market_sector_uptrend_stock_vs_sector_max_pct: float = 0.8
    market_sector_strong_up_pct: float = 2.5
    market_context_stock_vs_sector_sell_boost_pct: float = 3.0
    sell_near_session_high_pct: float = -0.8
    suspected_sell_near_session_high_pct: float = -1.2
    rally_fade_from_high_pct: float = 3.5
    rally_fade_min_intraday_high_pct: float = 4.0
    rally_fade_max_open_break_pct: float = -1.0
    opening_downtrend_break_open_pct: float = -2.5
    limit_break_fade_pct: float = 3.0
    limit_up_intraday_high_pct: float = 9.5
    behavior_high_valid_minutes: int = 120


class MarketContext(BaseModel):
    index_change_pct: float = 0
    sector_change_pct: float = 0
    sector_relative_strength_pct: float = 0
    stock_vs_sector_pct: float = 0
    sector_trend: str = "unknown"
    index_trend: str = "unknown"


def evaluate_signal(
    candles: list[Candle],
    index_candles: list[Candle],
    position: Position,
    health: ProviderHealth,
    now: datetime,
    thresholds: RuleThresholds | None = None,
    session_candles: list[Candle] | None = None,
    market_context: MarketContext | None = None,
) -> Signal:
    if not candles:
        return _hold(position.symbol, now, "暂无行情数据", source_fresh=False)

    if health.is_stale(now):
        return _hold(candles[-1].symbol, now, "数据延迟，暂停新信号", source_fresh=False)

    thresholds = thresholds or RuleThresholds()
    indicators = compute_indicators(candles, index_candles, session_candles)
    rule_ids: list[str] = []
    reasons: list[str] = []
    risks: list[str] = []

    if _sharp_drop_with_shrinking_volume(candles, indicators, thresholds):
        rule_ids.append("sharp_drop_shrinking_volume")
        reasons.append("急跌后量能收缩，并且价格明显低于 VWAP")
        risks.append("急跌后可能继续惯性下探")

    behavior_risks = _intraday_behavior_risks(candles, session_candles, indicators, thresholds)
    if behavior_risks and position.t_quantity > 0:
        risk_rule_ids = [risk.rule_id for risk in behavior_risks]
        risk_reasons = [risk.reason for risk in behavior_risks]
        risk_notes = [risk.risk for risk in behavior_risks]
        if "sharp_drop_shrinking_volume" in rule_ids:
            rule_ids = [rule_id for rule_id in rule_ids if rule_id != "sharp_drop_shrinking_volume"]
            reasons = [reason for reason in reasons if "急跌后量能收缩" not in reason]
            risks = [risk for risk in risks if "惯性下探" not in risk]
        rule_ids.extend(risk_rule_ids)
        reasons.extend(risk_reasons)
        risks.extend(risk_notes)

    if _vwap_high_sell(indicators, thresholds) and position.t_quantity > 0:
        rule_ids.append("vwap_high_sell")
        reasons.append("价格显著高于 VWAP，存在高抛候选")
        risks.append("强势股可能沿 VWAP 上方继续逼空")

    near_session_high = _near_session_high(candles, session_candles, thresholds.sell_near_session_high_pct)
    if (
        _intraday_gain_session_vwap_sell(indicators, thresholds)
        and near_session_high
        and position.t_quantity > 0
    ):
        rule_ids.append("intraday_gain_session_vwap_stretch")
        reasons.append("日内涨幅较大，并且价格显著高于全天均价，存在主动高抛候选")
        risks.append("强趋势日内仍可能继续上冲，分批高抛后需要预案回补")

    if indicators.relative_strength_pct <= thresholds.relative_weakness_pct and position.t_quantity > 0:
        rule_ids.append("relative_weakness")
        reasons.append("大盘走强但个股相对弱势")
        risks.append("弱势高抛后可能错过补涨")

    if rule_ids and market_context and _should_downgrade_sell_for_market_uptrend(
        market_context,
        thresholds,
    ):
        downgrade_reason = _market_uptrend_sell_downgrade_reason(market_context)
        return Signal(
            symbol=candles[-1].symbol,
            timestamp=candles[-1].timestamp,
            kind=SignalKind.SUSPECTED,
            action=SignalAction.SELL,
            confidence=0.52,
            rule_ids=[*rule_ids, "market_sector_uptrend_sell_downgrade"],
            reason="；".join(reasons) + f"；{downgrade_reason}",
            risks=[*risks, "板块主升时过早高抛可能卖飞"],
            source_fresh=True,
            llm_status="pending",
        )

    if rule_ids and market_context and _should_boost_sell_for_market_context(
        candles,
        market_context,
        thresholds,
    ):
        rule_ids.append("market_context_sell_boost")
        reasons.append("板块或大盘转弱时个股仍明显强于板块且接近日内高位，高抛优先级提高")
        risks.append("逆环境强势股可能继续抱团上冲")

    if not rule_ids and _deep_session_vwap_low_buy(candles, session_candles, indicators, thresholds):
        if _neway_weak_buy_guard(candles, session_candles, indicators):
            return _hold(candles[-1].symbol, candles[-1].timestamp, "新易盛深度回落低吸确认不足", source_fresh=True)
        return Signal(
            symbol=candles[-1].symbol,
            timestamp=candles[-1].timestamp,
            kind=SignalKind.SUSPECTED,
            action=SignalAction.BUY,
            confidence=0.52,
            rule_ids=["deep_session_vwap_low_buy"],
            reason="价格明显远离全天均价并接近日内低位，进入低吸观察区",
            risks=["深度偏离均价时可能继续杀跌，需等待止跌确认"],
            source_fresh=True,
            llm_status="pending",
        )

    if not rule_ids and _suspected_vwap_low_reversal(candles, indicators, thresholds):
        return Signal(
            symbol=candles[-1].symbol,
            timestamp=candles[-1].timestamp,
            kind=SignalKind.SUSPECTED,
            action=SignalAction.BUY,
            confidence=0.48,
            rule_ids=["suspected_vwap_low_reversal"],
            reason="价格回落到 VWAP 下方，跌幅接近低吸观察区",
            risks=["尚未达到强低吸规则，可能只是下跌中继"],
            source_fresh=True,
            llm_status="pending",
        )

    if not rule_ids and _pullback_low_rebound(candles, indicators, thresholds):
        if _neway_weak_buy_guard(candles, session_candles, indicators):
            return _hold(candles[-1].symbol, candles[-1].timestamp, "新易盛深度回落低吸确认不足", source_fresh=True)
        return Signal(
            symbol=candles[-1].symbol,
            timestamp=candles[-1].timestamp,
            kind=SignalKind.SUSPECTED,
            action=SignalAction.BUY,
            confidence=0.5,
            rule_ids=["pullback_low_rebound"],
            reason="价格回踩到全天均价下方，接近近期低位后出现缩量回抽",
            risks=["仍处于 VWAP 下方，若回抽失败可能继续探底"],
            source_fresh=True,
            llm_status="pending",
        )

    if (
        not rule_ids
        and _suspected_vwap_high_stretch(indicators, thresholds)
        and _near_session_high(candles, session_candles, thresholds.suspected_sell_near_session_high_pct)
        and position.t_quantity > 0
    ):
        return Signal(
            symbol=candles[-1].symbol,
            timestamp=candles[-1].timestamp,
            kind=SignalKind.SUSPECTED,
            action=SignalAction.SELL,
            confidence=0.46,
            rule_ids=["suspected_vwap_high_stretch"],
            reason="价格高于 VWAP，接近高抛观察区",
            risks=["强势拉升时可能继续上冲"],
            source_fresh=True,
            llm_status="pending",
        )

    if not rule_ids:
        return _hold(candles[-1].symbol, candles[-1].timestamp, "未满足候选条件", source_fresh=True)

    if "sharp_drop_shrinking_volume" in rule_ids and "vwap_high_sell" not in rule_ids:
        return Signal(
            symbol=candles[-1].symbol,
            timestamp=candles[-1].timestamp,
            kind=SignalKind.CANDIDATE_BUY,
            action=SignalAction.BUY,
            confidence=0.72,
            rule_ids=rule_ids,
            reason="；".join(reasons),
            risks=risks,
            source_fresh=True,
            llm_status="pending",
        )

    return Signal(
        symbol=candles[-1].symbol,
        timestamp=candles[-1].timestamp,
        kind=SignalKind.CANDIDATE_SELL,
        action=SignalAction.SELL,
        confidence=0.68,
        rule_ids=rule_ids,
        reason="；".join(reasons),
        risks=risks,
        source_fresh=True,
        llm_status="pending",
    )


class _BehaviorRisk(BaseModel):
    rule_id: str
    reason: str
    risk: str


def _intraday_behavior_risks(
    candles: list[Candle],
    session_candles: list[Candle] | None,
    indicators: IndicatorSnapshot,
    thresholds: RuleThresholds,
) -> list[_BehaviorRisk]:
    session = session_candles or candles
    if len(session) < 2:
        return []

    latest = session[-1]
    previous = session[-2]
    open_price = session[0].open
    high_candle = max(session, key=lambda candle: candle.high)
    high_so_far = high_candle.high
    high_change_pct = ((high_so_far - open_price) / open_price * 100) if open_price else 0
    fade_from_high_pct = ((latest.close - high_so_far) / high_so_far * 100) if high_so_far else 0
    previous_fade_from_high_pct = ((previous.close - high_so_far) / high_so_far * 100) if high_so_far else 0
    open_break_pct = ((latest.close - open_price) / open_price * 100) if open_price else 0
    previous_open_break_pct = ((previous.close - open_price) / open_price * 100) if open_price else 0
    high_is_recent = (latest.timestamp - high_candle.timestamp).total_seconds() <= thresholds.behavior_high_valid_minutes * 60
    risks: list[_BehaviorRisk] = []

    broke_open = latest.close < open_price
    just_broke_open = previous.close >= open_price and broke_open
    broke_session_vwap = indicators.price_session_vwap_deviation_pct < 0
    just_broke_session_vwap = previous.close >= indicators.session_vwap and broke_session_vwap
    still_falling = latest.close < previous.close
    rally_fade = (
        high_is_recent
        and high_change_pct >= thresholds.rally_fade_min_intraday_high_pct
        and fade_from_high_pct <= -thresholds.rally_fade_from_high_pct
        and open_break_pct >= thresholds.rally_fade_max_open_break_pct
        and (just_broke_open or (just_broke_session_vwap and still_falling))
    )
    if rally_fade:
        risks.append(
            _BehaviorRisk(
                rule_id="rally_fade_sell",
                reason="日内冲高后明显回落，T 仓先按高抛思路处理",
                risk="冲高承接转弱，过晚高抛可能把主动差价变成被动减仓",
            )
        )
        if broke_open:
            risks.append(
                _BehaviorRisk(
                    rule_id="break_open_after_rally",
                    reason="冲高回落后跌破开盘价，高抛优先级提高",
                    risk="跌破开盘价后可能继续向昨收或 VWAP 下方回落",
                )
            )
        if broke_session_vwap:
            risks.append(
                _BehaviorRisk(
                    rule_id="break_vwap_after_rally",
                    reason="冲高回落后跌破全天均价，低吸信号需要降级",
                    risk="跌破 VWAP 说明日内资金承接减弱",
                )
            )

    low_open_downtrend = (
        _is_opening_window(session)
        and not rally_fade
        and high_change_pct <= 1.2
        and open_break_pct <= thresholds.opening_downtrend_break_open_pct
        and previous_open_break_pct > thresholds.opening_downtrend_break_open_pct
        and _is_persistent_downtrend(session)
    )
    if low_open_downtrend:
        risks.append(
            _BehaviorRisk(
                rule_id="opening_downtrend_t_stop",
                reason="开盘后持续走弱并跌破开盘价 2.5%，T 仓进入底线减仓模式",
                risk="低开低走或开盘后下杀时，继续低吸可能扩大 T 仓风险",
            )
        )

    if (
        high_is_recent
        and high_change_pct >= thresholds.limit_up_intraday_high_pct
        and fade_from_high_pct <= -thresholds.limit_break_fade_pct
        and previous_fade_from_high_pct > -thresholds.limit_break_fade_pct
        and open_break_pct >= thresholds.rally_fade_max_open_break_pct
    ):
        risks.append(
            _BehaviorRisk(
                rule_id="limit_break_fade",
                reason="接近涨停后回落超过 3%，炸板风险触发",
                risk="炸板后资金分歧明显，不宜把回落直接当低吸",
            )
        )

    if _weak_rebound_session_vwap_fail(candles, session, indicators, thresholds):
        risks.append(
            _BehaviorRisk(
                rule_id="weak_rebound_session_vwap_fail",
                reason="弱势反抽均价失败，T 仓先按高抛思路处理",
                risk="反抽未能站稳全天均价，后续可能回落重测日内低点",
            )
        )

    return _dedupe_behavior_risks(risks)


def _is_persistent_downtrend(candles: list[Candle]) -> bool:
    if len(candles) < 4:
        return False
    closes = [candle.close for candle in candles[-4:]]
    lower_closes = all(left >= right for left, right in zip(closes, closes[1:]))
    latest = candles[-1]
    previous = candles[-2]
    return lower_closes and latest.close < previous.close


def _is_opening_window(candles: list[Candle]) -> bool:
    if not candles:
        return False
    first = candles[0].timestamp
    latest = candles[-1].timestamp
    return first.hour == 9 and first.minute <= 35 and (latest - first).total_seconds() <= 45 * 60


def _dedupe_behavior_risks(risks: list[_BehaviorRisk]) -> list[_BehaviorRisk]:
    deduped: list[_BehaviorRisk] = []
    seen: set[str] = set()
    for risk in risks:
        if risk.rule_id in seen:
            continue
        seen.add(risk.rule_id)
        deduped.append(risk)
    return deduped


def _sharp_drop_with_shrinking_volume(
    candles: list[Candle],
    indicators: IndicatorSnapshot,
    thresholds: RuleThresholds,
) -> bool:
    if len(candles) < 5:
        return False
    start = candles[-5].close
    latest = candles[-1].close
    drop_pct = (latest - start) / start * 100
    volumes = [candle.volume for candle in candles[-5:]]
    shrinking_volume = all(left > right for left, right in zip(volumes, volumes[1:]))
    return (
        drop_pct <= thresholds.sharp_drop_pct
        and shrinking_volume
        and indicators.price_vwap_deviation_pct <= thresholds.vwap_low_deviation_pct
    )


def _vwap_high_sell(indicators: IndicatorSnapshot, thresholds: RuleThresholds) -> bool:
    return indicators.price_vwap_deviation_pct >= thresholds.vwap_high_deviation_pct


def _intraday_gain_session_vwap_sell(
    indicators: IndicatorSnapshot,
    thresholds: RuleThresholds,
) -> bool:
    return (
        indicators.intraday_change_pct >= thresholds.intraday_gain_sell_pct
        and indicators.price_session_vwap_deviation_pct >= thresholds.session_vwap_high_deviation_pct
    )


def _suspected_vwap_low_reversal(
    candles: list[Candle],
    indicators: IndicatorSnapshot,
    thresholds: RuleThresholds,
) -> bool:
    if len(candles) < 5:
        return False
    start = candles[-5].close
    latest = candles[-1].close
    drop_pct = (latest - start) / start * 100
    return (
        (
            drop_pct <= thresholds.suspected_drop_pct
            and indicators.price_vwap_deviation_pct <= thresholds.suspected_vwap_low_deviation_pct
        )
        or indicators.price_vwap_deviation_pct <= thresholds.suspected_local_vwap_low_deviation_pct
    ) and _low_reversal_guard(candles)


def _pullback_low_rebound(
    candles: list[Candle],
    indicators: IndicatorSnapshot,
    thresholds: RuleThresholds,
) -> bool:
    if len(candles) < 5:
        return False

    latest = candles[-1]
    previous = candles[-2]
    recent_low = min(candle.low for candle in candles[-5:])
    near_recent_low_pct = ((latest.close - recent_low) / recent_low * 100) if recent_low else 0
    rebound_pct = ((latest.close - previous.close) / previous.close * 100) if previous.close else 0
    volume_not_expanding = latest.volume <= previous.volume

    return (
        indicators.price_session_vwap_deviation_pct <= thresholds.pullback_session_vwap_low_deviation_pct
        and near_recent_low_pct <= thresholds.pullback_near_recent_low_pct
        and rebound_pct >= thresholds.pullback_rebound_pct
        and volume_not_expanding
    )


def _deep_session_vwap_low_buy(
    candles: list[Candle],
    session_candles: list[Candle] | None,
    indicators: IndicatorSnapshot,
    thresholds: RuleThresholds,
) -> bool:
    session = session_candles or candles
    if not session:
        return False
    latest = session[-1]
    session_low = min(candle.low for candle in session)
    near_session_low_pct = ((latest.close - session_low) / session_low * 100) if session_low else 0
    previous_session = session[:-1]
    previous_low = min((candle.low for candle in previous_session), default=session_low)
    before_previous_low = min((candle.low for candle in previous_session[:-1]), default=previous_low)
    previous_indicators = compute_indicators(candles[:-1], [], previous_session) if len(candles) > 1 else None
    current_deep_deviation = indicators.price_session_vwap_deviation_pct <= thresholds.deep_session_vwap_low_deviation_pct
    previous_deep_deviation = bool(
        previous_indicators
        and previous_indicators.price_session_vwap_deviation_pct <= thresholds.deep_session_vwap_low_deviation_pct
    )
    crossed_deep_deviation = bool(
        previous_indicators
        and previous_indicators.price_session_vwap_deviation_pct > thresholds.deep_session_vwap_low_deviation_pct
        and current_deep_deviation
    )
    previous_candle_made_low = len(session) >= 2 and session[-2].low <= before_previous_low
    new_low_reversal = (latest.low <= previous_low or previous_candle_made_low) and _low_reversal_guard(candles)
    already_had_deep_low_event = _had_recent_deep_session_low_event(
        session,
        thresholds,
        exclude_lunch_break=True,
    )
    lunch_reopen_low_retest = (
        len(session) >= 2
        and session[-2].timestamp.strftime("%H:%M") <= "11:30"
        and latest.timestamp.strftime("%H:%M") >= "13:00"
        and near_session_low_pct <= thresholds.deep_session_near_low_pct
    )
    return (
        (current_deep_deviation or previous_deep_deviation)
        and near_session_low_pct <= thresholds.deep_session_near_low_pct
        and (crossed_deep_deviation or new_low_reversal or lunch_reopen_low_retest)
        and (not already_had_deep_low_event or lunch_reopen_low_retest)
    )


def _had_recent_deep_session_low_event(
    session_candles: list[Candle],
    thresholds: RuleThresholds,
    *,
    exclude_lunch_break: bool,
) -> bool:
    if len(session_candles) < 3:
        return False
    latest = session_candles[-1]
    prior_session = session_candles[:-1]
    for index in range(2, len(prior_session) + 1):
        current = prior_session[index - 1]
        if exclude_lunch_break and current.timestamp.strftime("%H:%M") <= "11:30" and latest.timestamp.strftime("%H:%M") >= "13:00":
            continue
        partial = session_candles[:index]
        window = partial[-30:]
        indicators = compute_indicators(window, [], partial)
        session_low = min(candle.low for candle in partial)
        near_session_low_pct = ((current.close - session_low) / session_low * 100) if session_low else 0
        if (
            indicators.price_session_vwap_deviation_pct <= thresholds.deep_session_vwap_low_deviation_pct
            and near_session_low_pct <= thresholds.deep_session_near_low_pct
            and _low_reversal_guard(window)
            and current.close > window[-2].close
        ):
            return True
    return False


def _weak_rebound_session_vwap_fail(
    candles: list[Candle],
    session_candles: list[Candle],
    indicators: IndicatorSnapshot,
    thresholds: RuleThresholds,
) -> bool:
    if len(session_candles) < 5 or len(candles) < 2:
        return False
    latest = session_candles[-1]
    previous = session_candles[-2]
    session_low = min(candle.low for candle in session_candles)
    rebound_from_low_pct = ((previous.close - session_low) / session_low * 100) if session_low else 0
    return (
        previous.close >= indicators.session_vwap
        and latest.close < indicators.session_vwap
        and latest.close < previous.close
        and indicators.intraday_change_pct <= thresholds.weak_rebound_vwap_fail_max_intraday_pct
        and rebound_from_low_pct >= thresholds.weak_rebound_min_rebound_from_low_pct
        and indicators.price_session_vwap_deviation_pct <= thresholds.weak_rebound_max_price_session_vwap_deviation_pct
    )


def _neway_weak_buy_guard(
    candles: list[Candle],
    session_candles: list[Candle] | None,
    indicators: IndicatorSnapshot,
) -> bool:
    if not candles or candles[-1].symbol != "300502":
        return False
    session = session_candles or candles
    if len(session) < 5:
        return False
    latest = session[-1]
    high_so_far = max(candle.high for candle in session)
    session_low = min(candle.low for candle in session)
    fade_from_high_pct = ((latest.close - high_so_far) / high_so_far * 100) if high_so_far else 0
    near_session_low_pct = ((latest.close - session_low) / session_low * 100) if session_low else 0
    previous = session[-2]
    rebound_pct = ((latest.close - previous.close) / previous.close * 100) if previous.close else 0
    extreme_capitulation_rebound = (
        fade_from_high_pct <= -10.0
        and indicators.price_session_vwap_deviation_pct <= -3.0
        and indicators.intraday_change_pct <= -6.0
        and near_session_low_pct <= 1.0
        and rebound_pct >= 0.6
        and latest.volume <= previous.volume
    )
    return (
        (
            fade_from_high_pct <= -6.0
            and indicators.price_session_vwap_deviation_pct <= -3.0
            and indicators.intraday_change_pct <= -3.0
            and not extreme_capitulation_rebound
        )
        or (
            indicators.intraday_change_pct <= -2.5
            and indicators.price_session_vwap_deviation_pct > -1.45
        )
    )


def _low_reversal_guard(candles: list[Candle]) -> bool:
    if len(candles) < 2:
        return False
    latest = candles[-1]
    previous = candles[-2]
    return latest.close >= previous.close or latest.volume <= previous.volume


def _suspected_vwap_high_stretch(
    indicators: IndicatorSnapshot,
    thresholds: RuleThresholds,
) -> bool:
    return (
        indicators.price_vwap_deviation_pct
        >= min(
            thresholds.suspected_vwap_high_deviation_pct,
            thresholds.suspected_local_vwap_high_deviation_pct,
        )
        or (
            indicators.intraday_change_pct >= thresholds.suspected_intraday_gain_sell_pct
            and indicators.price_session_vwap_deviation_pct
            >= thresholds.suspected_session_vwap_high_deviation_pct
        )
    )


def _should_downgrade_sell_for_market_uptrend(
    market_context: MarketContext,
    thresholds: RuleThresholds,
) -> bool:
    index_and_sector_up = (
        market_context.index_trend == "up"
        and market_context.sector_trend == "up"
        and market_context.sector_change_pct > market_context.index_change_pct
    )
    sector_strong_without_index = (
        market_context.index_trend == "unknown"
        and market_context.sector_trend == "up"
        and market_context.sector_change_pct >= thresholds.market_sector_strong_up_pct
    )
    return (
        (index_and_sector_up or sector_strong_without_index)
        and market_context.stock_vs_sector_pct <= thresholds.market_sector_uptrend_stock_vs_sector_max_pct
    )


def _market_uptrend_sell_downgrade_reason(market_context: MarketContext) -> str:
    if market_context.index_trend == "up":
        return "大盘与板块共振走强，个股并未明显强于板块，先降级为分批高抛观察"
    return "板块代理强势上行但缺少可用大盘指数数据，个股并未明显强于板块，先降级为分批高抛观察"


def _should_boost_sell_for_market_context(
    candles: list[Candle],
    market_context: MarketContext,
    thresholds: RuleThresholds,
) -> bool:
    if not candles:
        return False
    latest = candles[-1]
    high_so_far = max(candle.high for candle in candles)
    near_high_pct = ((latest.close - high_so_far) / high_so_far * 100) if high_so_far else 0
    environment_weakening = market_context.index_trend == "down" or market_context.sector_trend == "down"
    return (
        environment_weakening
        and market_context.stock_vs_sector_pct >= thresholds.market_context_stock_vs_sector_sell_boost_pct
        and near_high_pct >= -0.8
    )


def _near_session_high(
    candles: list[Candle],
    session_candles: list[Candle] | None,
    threshold_pct: float,
) -> bool:
    if not candles:
        return False
    latest = candles[-1]
    session = session_candles or candles
    high_so_far = max(candle.high for candle in session)
    near_high_pct = ((latest.close - high_so_far) / high_so_far * 100) if high_so_far else 0
    return near_high_pct >= threshold_pct


def _hold(symbol: str, timestamp: datetime, reason: str, source_fresh: bool) -> Signal:
    return Signal(
        symbol=symbol,
        timestamp=timestamp,
        kind=SignalKind.HOLD,
        action=SignalAction.HOLD,
        confidence=0,
        rule_ids=[],
        reason=reason,
        risks=[],
        source_fresh=source_fresh,
    )
