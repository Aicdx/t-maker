from __future__ import annotations

from tmaker.domain.models import Candle, Position, Signal
from tmaker.market.bars import aggregate_five_minute
from tmaker.strategy.indicators import compute_indicators


def build_review_context(
    signal: Signal,
    candles: list[Candle],
    position: Position,
    recent_signals: list[Signal],
    market_context: dict | None = None,
) -> dict:
    one_minute = candles[-60:]
    five_minute = aggregate_five_minute(candles)[-24:]
    indicators = compute_indicators(candles[-30:])

    return {
        "review_mode": "market_signal_first",
        "symbol": signal.symbol,
        "timestamp": signal.timestamp.isoformat(),
        "latest_price": candles[-1].close if candles else None,
        "instruction": (
            "先判断候选点本身是否构成盘中低吸/高抛市场信号；"
            "资金、底仓、最小交易单位只写入 execution_allowed/execution_blockers，"
            "不要因为账户不可执行而把市场动作强行改成 hold。"
        ),
        "candidate": signal.model_dump(mode="json", exclude={"llm_review"}),
        "indicators": indicators.model_dump(mode="json"),
        "market_context": market_context,
        "position": position.model_dump(mode="json"),
        "one_minute_candles": [candle.model_dump(mode="json") for candle in one_minute],
        "five_minute_candles": [candle.model_dump(mode="json") for candle in five_minute],
        "recent_signals": [
            item.model_dump(mode="json", exclude={"llm_review"})
            for item in recent_signals
            if item.symbol == signal.symbol
            and item.timestamp <= signal.timestamp
        ][-5:],
        "risk_filters": [
            "仅做盘中决策辅助，不自动下单",
            "靠近收盘或数据延迟时应降低信号权重",
            "需要人工确认成交价格和仓位",
        ],
    }
