from datetime import datetime

import pytest
from pydantic import ValidationError

from tmaker.config import Settings
from tmaker.domain.models import LlmReview, Signal, SignalAction, SignalKind
from tmaker.monitor.policy import MonitorPolicy, signal_notification_key


def _signal(
    *,
    kind: SignalKind = SignalKind.CANDIDATE_BUY,
    action: SignalAction = SignalAction.BUY,
    llm_action: SignalAction = SignalAction.BUY,
    confidence: float = 0.64,
    source_fresh: bool = True,
    llm_status: str = "ok",
) -> Signal:
    review = LlmReview(
        action=llm_action,
        confidence=confidence,
        summary="AI 确认可作为低吸观察点",
        reasons=["价格低于 VWAP"],
        risks=["趋势仍弱"],
        wait_for=["下一根 1 分钟 K 线不破低点"],
        execution_allowed=True,
        execution_blockers=[],
    )
    return Signal(
        symbol="300308",
        timestamp=datetime(2026, 6, 9, 10, 23),
        kind=kind,
        action=action,
        confidence=0.72,
        rule_ids=["vwap_low_deviation"],
        reason="低位偏离均价线",
        risks=["可能继续下探"],
        source_fresh=source_fresh,
        llm_status=llm_status,
        llm_review=review,
    )


def test_policy_accepts_ai_confirmed_buy_above_threshold() -> None:
    policy = MonitorPolicy(min_ai_confidence=0.6)

    assert policy.should_notify(_signal()) is True


def test_policy_rejects_hold_by_default() -> None:
    policy = MonitorPolicy(min_ai_confidence=0.6)

    assert policy.should_notify(_signal(llm_action=SignalAction.HOLD)) is False


def test_policy_can_notify_hold_when_enabled_and_above_threshold() -> None:
    policy = MonitorPolicy(min_ai_confidence=0.6, notify_hold=True)

    assert policy.should_notify(_signal(llm_action=SignalAction.HOLD)) is True


def test_policy_rejects_stale_source_signal() -> None:
    policy = MonitorPolicy(min_ai_confidence=0.6)

    assert policy.should_notify(_signal(source_fresh=False)) is False


def test_policy_rejects_failed_or_pending_review() -> None:
    policy = MonitorPolicy(min_ai_confidence=0.6)

    assert policy.should_notify(_signal(llm_status="failed")) is False
    assert policy.should_notify(_signal(llm_status="pending")) is False


def test_policy_rejects_low_ai_confidence() -> None:
    policy = MonitorPolicy(min_ai_confidence=0.7)

    assert policy.should_notify(_signal(confidence=0.69)) is False


def test_policy_can_disable_suspected_signals() -> None:
    policy = MonitorPolicy(notify_suspected=False)

    assert policy.should_notify(_signal(kind=SignalKind.SUSPECTED)) is False


def test_signal_notification_key_changes_when_ai_result_changes() -> None:
    buy_key = signal_notification_key(_signal(llm_action=SignalAction.BUY, confidence=0.641))
    sell_key = signal_notification_key(_signal(llm_action=SignalAction.SELL, confidence=0.641))
    rounded_key = signal_notification_key(_signal(llm_action=SignalAction.BUY, confidence=0.644))

    assert buy_key != sell_key
    assert buy_key == rounded_key


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("monitor_interval_seconds", 0),
        ("monitor_min_ai_confidence", -0.01),
        ("monitor_min_ai_confidence", 1.01),
        ("monitor_dedup_window_minutes", 0),
        ("feishu_timeout_seconds", 0),
    ],
)
def test_monitor_settings_reject_invalid_numeric_values(
    field_name: str, invalid_value: float
) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field_name: invalid_value})
