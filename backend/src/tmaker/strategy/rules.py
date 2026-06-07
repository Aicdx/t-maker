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
    market_sector_uptrend_stock_vs_sector_max_pct: float = 0.8
    market_sector_strong_up_pct: float = 2.5
    market_context_stock_vs_sector_sell_boost_pct: float = 3.0
    sell_near_session_high_pct: float = -0.8
    suspected_sell_near_session_high_pct: float = -1.2


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
    )


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
