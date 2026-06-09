from __future__ import annotations

from dataclasses import dataclass

from tmaker.domain.models import Signal, SignalAction, SignalKind


@dataclass(frozen=True)
class MonitorPolicy:
    min_ai_confidence: float = 0.6
    notify_hold: bool = False
    notify_suspected: bool = True

    def should_notify(self, signal: Signal) -> bool:
        if signal.kind == SignalKind.HOLD:
            return False
        if signal.kind == SignalKind.SUSPECTED and not self.notify_suspected:
            return False
        if not signal.source_fresh:
            return False
        if signal.llm_status != "ok" or signal.llm_review is None:
            return False
        if signal.llm_review.confidence < self.min_ai_confidence:
            return False
        if signal.llm_review.action == SignalAction.HOLD and not self.notify_hold:
            return False
        return signal.llm_review.action in {
            SignalAction.BUY,
            SignalAction.SELL,
            SignalAction.HOLD,
        }


def signal_notification_key(signal: Signal) -> str:
    review = signal.llm_review
    llm_action = review.action.value if review else "none"
    llm_confidence = round(review.confidence, 2) if review else 0
    return "|".join(
        [
            signal.symbol,
            signal.timestamp.isoformat(),
            signal.action.value,
            llm_action,
            f"{llm_confidence:.2f}",
        ]
    )
