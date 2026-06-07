from datetime import datetime

import pytest
from pydantic import ValidationError

from tmaker.domain.models import (
    Candle,
    LlmReview,
    Position,
    ProviderHealth,
    Signal,
    SignalAction,
    SignalKind,
)


def test_candle_rejects_negative_ohlcv_values() -> None:
    with pytest.raises(ValidationError):
        Candle(
            symbol="600000",
            timestamp=datetime(2026, 6, 5, 9, 31),
            open=10,
            high=10.2,
            low=9.9,
            close=-1,
            volume=1200,
        )


def test_candle_serializes_timestamp_as_iso_string() -> None:
    candle = Candle(
        symbol="600000",
        timestamp=datetime(2026, 6, 5, 9, 31),
        open=10,
        high=10.2,
        low=9.9,
        close=10.1,
        volume=1200,
    )

    payload = candle.model_dump(mode="json")

    assert payload["timestamp"] == "2026-06-05T09:31:00"
    assert payload["symbol"] == "600000"


def test_position_rejects_negative_quantity_and_cash() -> None:
    with pytest.raises(ValidationError):
        Position(symbol="600000", base_quantity=-100, cost_price=10, available_cash=1000, t_quantity=100)

    with pytest.raises(ValidationError):
        Position(symbol="600000", base_quantity=100, cost_price=10, available_cash=-1, t_quantity=100)


def test_provider_health_marks_stale_data() -> None:
    health = ProviderHealth(
        provider="akshare",
        symbol="600000",
        last_success_at=datetime(2026, 6, 5, 9, 31),
        latency_ms=230,
        stale_after_seconds=60,
    )

    assert health.is_stale(datetime(2026, 6, 5, 9, 32, 1)) is True
    assert health.status_at(datetime(2026, 6, 5, 9, 32, 1)) == "data_delayed"


def test_provider_health_preserves_last_error_message() -> None:
    health = ProviderHealth(
        provider="tencent_ifzq_fallback",
        symbol="600000",
        last_error="Remote end closed connection without response",
    )

    assert health.model_dump()["last_error"] == "Remote end closed connection without response"


def test_signal_requires_llm_review_only_for_candidates() -> None:
    candidate = Signal(
        symbol="600000",
        timestamp=datetime(2026, 6, 5, 10, 15),
        kind=SignalKind.CANDIDATE_BUY,
        action=SignalAction.BUY,
        confidence=0.72,
        rule_ids=["sharp_drop_shrinking_volume"],
        reason="急跌后量能收缩并低于 VWAP",
        risks=["免费行情源可能延迟"],
        source_fresh=True,
    )
    hold = Signal(
        symbol="600000",
        timestamp=datetime(2026, 6, 5, 10, 16),
        kind=SignalKind.HOLD,
        action=SignalAction.HOLD,
        confidence=0.2,
        rule_ids=[],
        reason="未满足候选条件",
        risks=[],
        source_fresh=True,
    )

    assert candidate.needs_llm_review is True
    assert hold.needs_llm_review is False


def test_llm_review_confidence_must_be_between_zero_and_one() -> None:
    with pytest.raises(ValidationError):
        LlmReview(
            action=SignalAction.BUY,
            confidence=1.5,
            summary="too confident",
            reasons=["reason"],
            risks=[],
            wait_for=[],
        )


def test_llm_review_keeps_execution_feasibility_separate_from_market_action() -> None:
    review = LlmReview(
        action=SignalAction.BUY,
        confidence=0.72,
        summary="市场低吸点成立，但账户资金不足，执行层面不能直接下单",
        reasons=["价格明显低于 VWAP", "1 分钟急跌后出现承接"],
        risks=["仍可能继续下探"],
        wait_for=["等待下一根 1 分钟 K 线不破低点"],
        execution_allowed=False,
        execution_blockers=["可用资金不足以买入计划 100 股"],
    )

    assert review.action == SignalAction.BUY
    assert review.execution_allowed is False
    assert review.execution_blockers == ["可用资金不足以买入计划 100 股"]
