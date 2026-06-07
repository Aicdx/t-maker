from datetime import datetime

import pytest

from tmaker.domain.models import Signal, SignalAction, SignalKind
from tmaker.llm.review import LlmReviewer


class FakeClient:
    def __init__(self, payload: dict | Exception) -> None:
        self.payload = payload
        self.calls = 0

    async def create_review(self, context: dict) -> dict:
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def candidate_signal() -> Signal:
    return Signal(
        symbol="600000",
        timestamp=datetime(2026, 6, 5, 10, 15),
        kind=SignalKind.CANDIDATE_BUY,
        action=SignalAction.BUY,
        confidence=0.7,
        rule_ids=["sharp_drop_shrinking_volume"],
        reason="急跌量缩",
        risks=["可能继续下探"],
        source_fresh=True,
    )


@pytest.mark.asyncio
async def test_reviewer_attaches_structured_llm_review_for_candidate() -> None:
    client = FakeClient(
        {
            "action": "buy",
            "confidence": 0.62,
            "summary": "可低吸但等待企稳",
            "reasons": ["价格低于 VWAP", "量能收缩"],
            "risks": ["大盘走弱"],
            "wait_for": ["下一根 1 分钟 K 线不创新低"],
        }
    )
    reviewer = LlmReviewer(client)

    reviewed = await reviewer.review(candidate_signal(), {"candles": []})

    assert client.calls == 1
    assert reviewed.llm_status == "ok"
    assert reviewed.llm_review is not None
    assert reviewed.llm_review.action == SignalAction.BUY


@pytest.mark.asyncio
async def test_reviewer_does_not_call_model_for_hold_signal() -> None:
    client = FakeClient({})
    reviewer = LlmReviewer(client)
    signal = Signal(
        symbol="600000",
        timestamp=datetime(2026, 6, 5, 10, 15),
        kind=SignalKind.HOLD,
        action=SignalAction.HOLD,
        confidence=0,
        rule_ids=[],
        reason="观望",
        risks=[],
        source_fresh=True,
    )

    reviewed = await reviewer.review(signal, {"candles": []})

    assert client.calls == 0
    assert reviewed.llm_status == "not_requested"


@pytest.mark.asyncio
async def test_reviewer_marks_failure_when_model_raises() -> None:
    client = FakeClient(RuntimeError("network"))
    reviewer = LlmReviewer(client)

    reviewed = await reviewer.review(candidate_signal(), {"candles": []})

    assert reviewed.llm_status == "failed"
    assert reviewed.llm_review is not None
    assert reviewed.llm_review.action == SignalAction.HOLD
    assert "模型复核失败" in reviewed.llm_review.summary
    assert reviewed.llm_review.execution_allowed is False


@pytest.mark.asyncio
async def test_reviewer_marks_failure_for_unexpected_client_errors() -> None:
    client = FakeClient(ConnectionError("proxy unavailable"))
    reviewer = LlmReviewer(client)

    reviewed = await reviewer.review(candidate_signal(), {"candles": []})

    assert reviewed.llm_status == "failed"
    assert reviewed.llm_review is not None
    assert "proxy unavailable" in reviewed.llm_review.execution_blockers[0]
