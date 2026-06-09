import json
from datetime import datetime

import httpx
import pytest

from tmaker.domain.models import LlmReview, MarketQuote, Signal, SignalAction, SignalKind
from tmaker.notify.feishu import FeishuConfigError, FeishuNotifier, format_feishu_message


def _signal() -> Signal:
    return Signal(
        symbol="300308",
        timestamp=datetime(2026, 6, 9, 10, 23),
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
            reasons=["偏离均价线", "量能收缩"],
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


def test_format_feishu_message_includes_engineering_and_codex_analysis() -> None:
    message = format_feishu_message(
        signal=_signal(),
        quote=_quote(),
        codex_analysis={
            "judgement": "buy",
            "summary": "更像观察型低吸，等待不破低点后再提高确认度。",
            "key_levels": ["支撑 121.80", "压力 125.00"],
            "next_steps": ["观察下一根 1 分钟 K 线是否站回均价线"],
            "invalidates": ["跌破 121.80"],
            "risk_notes": ["高波动品种仓位要轻"],
        },
    )

    assert "【T Maker 盯盘复核】中际旭创 300308" in message
    assert "工程 AI：低吸，68%" in message
    assert "候选点具备低吸观察价值" in message
    assert "Codex 二次判断：" in message
    assert "更像观察型低吸" in message
    assert "提醒：仅供盘中辅助判断，不自动下单。" in message


@pytest.mark.asyncio
async def test_feishu_notifier_posts_text_message() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"StatusCode": 0})

    notifier = FeishuNotifier(
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        transport=httpx.MockTransport(handler),
    )

    await notifier.send_text("hello")

    assert seen["url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    assert seen["body"] == {"msg_type": "text", "content": {"text": "hello"}}


@pytest.mark.asyncio
async def test_feishu_notifier_requires_webhook_url() -> None:
    notifier = FeishuNotifier(webhook_url="")

    with pytest.raises(FeishuConfigError, match="FEISHU_WEBHOOK_URL"):
        await notifier.send_text("hello")


@pytest.mark.asyncio
async def test_feishu_notifier_raises_on_failed_response() -> None:
    notifier = FeishuNotifier(
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="server error")),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await notifier.send_text("hello")
