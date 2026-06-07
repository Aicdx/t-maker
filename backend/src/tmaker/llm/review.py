from __future__ import annotations

from typing import Protocol

from tmaker.domain.models import LlmReview, Signal, SignalAction


class ReviewClient(Protocol):
    async def create_review(self, context: dict) -> dict:
        """Return a structured model-review payload."""


class LlmReviewer:
    def __init__(self, client: ReviewClient) -> None:
        self.client = client

    async def review(self, signal: Signal, context: dict) -> Signal:
        if not signal.needs_llm_review:
            return signal.model_copy(update={"llm_status": "not_requested"})

        try:
            payload = await self.client.create_review(context)
            review = LlmReview.model_validate(payload)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            review = LlmReview(
                action=SignalAction.HOLD,
                confidence=0,
                summary="模型复核失败，未形成有效 AI 买卖判断",
                reasons=[],
                risks=["AI 复核不可用时不能把该结果视为买卖确认"],
                wait_for=["稍后重试 AI 复核，或仅按规则候选人工判断"],
                execution_allowed=False,
                execution_blockers=[f"模型复核失败：{message[:180]}"],
            )
            return signal.model_copy(update={"llm_status": "failed", "llm_review": review})

        return signal.model_copy(update={"llm_status": "ok", "llm_review": review})
