from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import TypeAdapter

from tmaker.domain.models import MarketQuote, Signal
from tmaker.llm.codex_analysis import fallback_codex_analysis
from tmaker.monitor.policy import MonitorPolicy, signal_notification_key
from tmaker.monitor.state import MonitorRuntimeState
from tmaker.notify.feishu import format_feishu_message


class SnapshotService(Protocol):
    def refresh(self) -> dict:
        """Refresh market state and return the snapshot payload."""


class SignalAnalyzer(Protocol):
    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        """Return Codex-style second analysis."""


class TextNotifier(Protocol):
    async def send_text(self, text: str) -> None:
        """Send one text notification."""

    def send_text_sync(self, text: str) -> None:
        """Send one text notification from sync endpoints."""


class MonitorRunner:
    def __init__(
        self,
        *,
        snapshot_service: SnapshotService,
        analyzer: SignalAnalyzer,
        notifier: TextNotifier,
        policy: MonitorPolicy,
        interval_seconds: float = 30,
        dedup_window_minutes: int = 5,
        notifications_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.snapshot_service = snapshot_service
        self.analyzer = analyzer
        self.notifier = notifier
        self.policy = policy
        self.interval_seconds = interval_seconds
        self.dedup_window = timedelta(minutes=dedup_window_minutes)
        self.notifications_enabled = notifications_enabled or (lambda: True)
        self.state = MonitorRuntimeState()
        self._task: asyncio.Task[None] | None = None
        self._notified_keys: dict[str, datetime] = {}
        self._sent_keys: set[str] = set()
        self._silenced_keys: set[str] = set()

    async def start(self, *, silence_existing: bool = False) -> None:
        if self._task and not self._task.done():
            self.state.running = True
            return
        if silence_existing:
            await self.silence_current_signals()
        self.state.running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.state.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def tick(self, *, now: datetime | None = None, force: bool = False) -> None:
        current = now or datetime.now()
        self.state.last_tick_at = current
        if not force and not is_ashare_trading_time(current):
            return
        try:
            payload = await asyncio.to_thread(self.snapshot_service.refresh)
            await self._notify_payload(payload, current)
            self.state.last_success_at = current
            self.state.last_error = None
        except Exception as exc:
            self.state.last_error = str(exc).strip() or exc.__class__.__name__

    async def _run_loop(self) -> None:
        while self.state.running:
            await self.tick()
            await asyncio.sleep(self.interval_seconds)

    async def silence_current_signals(self) -> None:
        try:
            payload = await asyncio.to_thread(self.snapshot_service.refresh)
            self._silence_payload(payload)
            self.state.last_error = None
        except Exception as exc:
            self.state.last_error = str(exc).strip() or exc.__class__.__name__

    def _silence_payload(self, payload: dict[str, Any]) -> None:
        signal_adapter = TypeAdapter(list[Signal])
        signals = signal_adapter.validate_python(payload.get("signals", []))
        for signal in signals:
            if self.policy.should_notify(signal):
                self._silenced_keys.add(signal_notification_key(signal))

    async def _notify_payload(self, payload: dict[str, Any], now: datetime) -> None:
        signal_adapter = TypeAdapter(list[Signal])
        quote_adapter = TypeAdapter(dict[str, MarketQuote])
        signals = signal_adapter.validate_python(payload.get("signals", []))
        quotes = quote_adapter.validate_python(payload.get("quotes", {}))
        self._prune_dedup(now)
        for signal in signals:
            if not self.policy.should_notify(signal):
                continue
            key = signal_notification_key(signal)
            if key in self._silenced_keys:
                continue
            if key in self._sent_keys:
                continue
            if key in self._notified_keys:
                continue
            if not self.notifications_enabled():
                continue
            quote = quotes.get(signal.symbol)
            try:
                analysis = await self.analyzer.analyze(
                    {
                        "signal": signal.model_dump(mode="json"),
                        "quote": quote.model_dump(mode="json") if quote else None,
                        "candles": payload.get("candles", []),
                        "chart_series": payload.get("chart_series", {}),
                        "positions": payload.get("positions", []),
                        "provider_health": payload.get("provider_health"),
                    }
                )
            except Exception as exc:
                analysis = fallback_codex_analysis(exc)
            message = format_feishu_message(signal=signal, quote=quote, codex_analysis=analysis)
            await self.notifier.send_text(message)
            self._sent_keys.add(key)
            self._notified_keys[key] = now
            self.state.notification_count += 1
            self.state.last_notified_signal_key = key

    def _prune_dedup(self, now: datetime) -> None:
        expired = [
            key
            for key, notified_at in self._notified_keys.items()
            if now - notified_at > self.dedup_window
        ]
        for key in expired:
            self._notified_keys.pop(key, None)


def is_ashare_trading_time(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (
        13 * 60 <= minutes <= 15 * 60
    )
