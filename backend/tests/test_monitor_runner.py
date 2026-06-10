import asyncio
from datetime import datetime

import pytest

from tmaker.domain.models import LlmReview, MarketQuote, Signal, SignalAction, SignalKind
from tmaker.monitor.policy import MonitorPolicy
from tmaker.monitor.runner import MonitorRunner


def _signal(timestamp: datetime = datetime(2026, 6, 9, 10, 23)) -> Signal:
    return Signal(
        symbol="300308",
        timestamp=timestamp,
        kind=SignalKind.CANDIDATE_BUY,
        action=SignalAction.BUY,
        confidence=0.72,
        rule_ids=["vwap_low_deviation"],
        reason="价格低于 VWAP 且卖压收缩",
        risks=["大盘走弱时可能继续下探"],
        source_fresh=True,
        llm_status="ok",
        llm_review=LlmReview(
            action=SignalAction.BUY,
            confidence=0.68,
            summary="候选点具备低吸观察价值",
            reasons=["偏离均价线"],
            risks=["仍在弱势结构内"],
            wait_for=["下一根 1 分钟 K 线不创新低"],
            execution_allowed=True,
            execution_blockers=[],
        ),
    )


def _quote() -> MarketQuote:
    return MarketQuote(
        symbol="300308",
        name="中际旭创",
        latest=123.45,
        previous_close=121.0,
        open=122.0,
        high=125.0,
        low=120.5,
        change=2.45,
        change_percent=2.02,
    )


class FakeSnapshotService:
    def __init__(self, signals: list[Signal]) -> None:
        self.signals = signals
        self.calls = 0

    def refresh(self) -> dict:
        self.calls += 1
        return {
            "signals": [signal.model_dump(mode="json") for signal in self.signals],
            "quotes": {"300308": _quote().model_dump(mode="json")},
            "candles": [],
            "chart_series": {"realtime": [], "one_minute": [], "five_minute": []},
            "positions": [],
            "provider_health": {
                "provider": "test",
                "symbol": "300308",
                "last_success_at": "2026-06-09T10:23:00",
                "latency_ms": 0,
                "stale_after_seconds": 90,
                "missing_candle_count": 0,
                "last_error": None,
            },
        }


class AsyncioRunSnapshotService:
    def refresh(self) -> dict:
        async def _inner() -> dict:
            return {"signals": [], "quotes": {}}

        return asyncio.run(_inner())


class FakeAnalyzer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def analyze(self, context: dict) -> dict:
        self.calls.append(context)
        if self.fail:
            raise RuntimeError("analysis failed")
        return {
            "judgement": "buy",
            "summary": "等待不破低点后再提高确认度。",
            "key_levels": ["支撑 121.80"],
            "next_steps": ["观察下一根 1 分钟 K 线"],
            "invalidates": ["跌破 121.80"],
            "risk_notes": ["高波动品种仓位要轻"],
        }


class FakeNotifier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[str] = []

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("feishu failed")
        self.messages.append(text)


@pytest.mark.asyncio
async def test_tick_sends_one_notification_for_eligible_signal() -> None:
    snapshot = FakeSnapshotService([_signal()])
    analyzer = FakeAnalyzer()
    notifier = FakeNotifier()
    runner = MonitorRunner(
        snapshot_service=snapshot,
        analyzer=analyzer,
        notifier=notifier,
        policy=MonitorPolicy(min_ai_confidence=0.6),
        dedup_window_minutes=240,
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)

    assert snapshot.calls == 1
    assert len(analyzer.calls) == 1
    context = analyzer.calls[0]
    assert context["signal"]["symbol"] == "300308"
    assert context["quote"]["name"] == "中际旭创"
    assert context["candles"] == []
    assert context["chart_series"] == {
        "realtime": [],
        "one_minute": [],
        "five_minute": [],
    }
    assert context["positions"] == []
    assert context["provider_health"] == {
        "provider": "test",
        "symbol": "300308",
        "last_success_at": "2026-06-09T10:23:00",
        "latency_ms": 0,
        "stale_after_seconds": 90,
        "missing_candle_count": 0,
        "last_error": None,
    }
    assert len(notifier.messages) == 1
    assert "Codex 二次判断" in notifier.messages[0]
    assert runner.state.notification_count == 1
    assert runner.state.last_error is None


@pytest.mark.asyncio
async def test_tick_skips_feishu_when_notifications_are_disabled() -> None:
    analyzer = FakeAnalyzer()
    notifier = FakeNotifier()
    runner = MonitorRunner(
        snapshot_service=FakeSnapshotService([_signal()]),
        analyzer=analyzer,
        notifier=notifier,
        policy=MonitorPolicy(min_ai_confidence=0.6),
        notifications_enabled=lambda: False,
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)

    assert analyzer.calls == []
    assert notifier.messages == []
    assert runner.state.notification_count == 0
    assert runner.state.last_error is None


def test_monitor_runner_defaults_to_five_minute_dedup_window() -> None:
    runner = MonitorRunner(
        snapshot_service=FakeSnapshotService([]),
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(),
        policy=MonitorPolicy(min_ai_confidence=0.6),
    )

    assert runner.dedup_window.total_seconds() == 5 * 60


@pytest.mark.asyncio
async def test_tick_runs_sync_snapshot_refresh_outside_event_loop() -> None:
    runner = MonitorRunner(
        snapshot_service=AsyncioRunSnapshotService(),
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(),
        policy=MonitorPolicy(min_ai_confidence=0.6),
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)

    assert runner.state.last_error is None


@pytest.mark.asyncio
async def test_tick_deduplicates_repeated_signal() -> None:
    snapshot = FakeSnapshotService([_signal()])
    runner = MonitorRunner(
        snapshot_service=snapshot,
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(),
        policy=MonitorPolicy(min_ai_confidence=0.6),
        dedup_window_minutes=240,
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)
    await runner.tick(now=datetime(2026, 6, 9, 10, 25), force=True)

    assert runner.state.notification_count == 1


@pytest.mark.asyncio
async def test_tick_allows_notification_after_dedup_window_expires() -> None:
    runner = MonitorRunner(
        snapshot_service=FakeSnapshotService([_signal()]),
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(),
        policy=MonitorPolicy(min_ai_confidence=0.6),
        dedup_window_minutes=1,
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)
    assert runner.state.notification_count == 1

    await runner.tick(now=datetime(2026, 6, 9, 10, 24, 30), force=True)
    assert runner.state.notification_count == 1

    await runner.tick(now=datetime(2026, 6, 9, 10, 26), force=True)
    assert runner.state.notification_count == 2


@pytest.mark.asyncio
async def test_tick_keeps_signal_retryable_when_feishu_fails() -> None:
    runner = MonitorRunner(
        snapshot_service=FakeSnapshotService([_signal()]),
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(fail=True),
        policy=MonitorPolicy(min_ai_confidence=0.6),
        dedup_window_minutes=240,
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)

    assert runner.state.notification_count == 0
    assert runner.state.last_notified_signal_key is None
    assert "feishu failed" in (runner.state.last_error or "")


@pytest.mark.asyncio
async def test_tick_sends_fallback_message_when_analysis_fails() -> None:
    notifier = FakeNotifier()
    runner = MonitorRunner(
        snapshot_service=FakeSnapshotService([_signal()]),
        analyzer=FakeAnalyzer(fail=True),
        notifier=notifier,
        policy=MonitorPolicy(min_ai_confidence=0.6),
        dedup_window_minutes=240,
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)

    assert runner.state.notification_count == 1
    assert "Codex 二次分析暂不可用" in notifier.messages[0]


@pytest.mark.asyncio
async def test_tick_skips_outside_trading_time_without_force() -> None:
    snapshot = FakeSnapshotService([_signal()])
    runner = MonitorRunner(
        snapshot_service=snapshot,
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(),
        policy=MonitorPolicy(min_ai_confidence=0.6),
        dedup_window_minutes=240,
    )

    await runner.tick(now=datetime(2026, 6, 9, 12, 0), force=False)

    assert snapshot.calls == 0
    assert runner.state.notification_count == 0


@pytest.mark.asyncio
async def test_start_stop_are_idempotent() -> None:
    runner = MonitorRunner(
        snapshot_service=FakeSnapshotService([]),
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(),
        policy=MonitorPolicy(min_ai_confidence=0.6),
        interval_seconds=3600,
    )

    await runner.start()
    first_task = runner._task
    await runner.start()

    assert runner.state.running is True
    assert runner._task is first_task

    await runner.stop()
    await runner.stop()

    assert runner.state.running is False
