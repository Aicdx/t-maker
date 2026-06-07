from datetime import datetime

from tmaker.domain.models import Candle, Position, Signal, SignalAction, SignalKind
from tmaker.llm.context import build_review_context


def test_review_context_excludes_future_recent_signals() -> None:
    candidate = _signal(datetime(2026, 6, 5, 10, 10), SignalAction.BUY)
    past = _signal(datetime(2026, 6, 5, 10, 9), SignalAction.SELL)
    future = _signal(datetime(2026, 6, 5, 13, 31), SignalAction.BUY)
    candles = [
        Candle(
            symbol="300308",
            timestamp=datetime(2026, 6, 5, 10, minute),
            open=10,
            high=10.2,
            low=9.8,
            close=10,
            volume=1000,
        )
        for minute in range(6, 11)
    ]

    context = build_review_context(
        candidate,
        candles,
        Position(symbol="300308", base_quantity=0, cost_price=0, available_cash=20000, t_quantity=100),
        [past, candidate, future],
    )

    assert context["review_mode"] == "market_signal_first"
    assert [item["timestamp"] for item in context["recent_signals"]] == [
        "2026-06-05T10:09:00",
        "2026-06-05T10:10:00",
    ]


def test_review_context_includes_restore_base_position_rule_reason() -> None:
    candidate = _signal(datetime(2026, 6, 5, 10, 10), SignalAction.BUY).model_copy(
        update={
            "rule_ids": ["suspected_vwap_low_reversal", "restore_after_intraday_sell"],
            "reason": "价格回落到 VWAP 下方；已高抛导致日内持仓低于底仓，后续低吸应优先用于回补底仓",
        }
    )
    candles = [
        Candle(
            symbol="300308",
            timestamp=datetime(2026, 6, 5, 10, minute),
            open=10,
            high=10.2,
            low=9.8,
            close=10,
            volume=1000,
        )
        for minute in range(6, 11)
    ]

    context = build_review_context(
        candidate,
        candles,
        Position(symbol="300308", base_quantity=200, cost_price=0, available_cash=20000, t_quantity=100),
        [candidate],
    )

    assert "restore_after_intraday_sell" in context["candidate"]["rule_ids"]
    assert "回补底仓" in context["candidate"]["reason"]
    assert context["position"]["base_quantity"] == 200


def _signal(timestamp: datetime, action: SignalAction) -> Signal:
    return Signal(
        symbol="300308",
        timestamp=timestamp,
        kind=SignalKind.SUSPECTED,
        action=action,
        confidence=0.48,
        rule_ids=["suspected_vwap_low_reversal"],
        reason="疑似低吸观察点",
        risks=[],
        source_fresh=True,
    )
