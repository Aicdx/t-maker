from __future__ import annotations

from collections.abc import Iterable
import asyncio
from typing import Any

import httpx

from tmaker.domain.models import MarketQuote, Signal, SignalAction
from tmaker.strategy.replay import ReplayPoint


class FeishuConfigError(RuntimeError):
    """Raised when Feishu notification is not configured."""


class FeishuDeliveryError(RuntimeError):
    """Raised when Feishu rejects a notification delivery."""


class FeishuNotifier:
    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: float = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def send_text(self, text: str) -> None:
        if not self.webhook_url:
            raise FeishuConfigError("FEISHU_WEBHOOK_URL is not configured")
        payload = {"msg_type": "text", "content": {"text": text}}
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = await client.post(self.webhook_url, json=payload)
            response.raise_for_status()
        _raise_for_feishu_error(response)

    def send_text_sync(self, text: str) -> None:
        asyncio.run(self.send_text(text))


def format_feishu_message(
    signal: Signal,
    quote: MarketQuote | None,
    codex_analysis: dict[str, Any] | None,
) -> str:
    review = signal.llm_review
    name = quote.name if quote else signal.symbol
    latest = quote.latest if quote else None
    lines = [
        f"【T Maker 盯盘复核】{name} {signal.symbol}",
        "",
        f"信号：{_kind_label(signal)}",
        f"时间：{signal.timestamp.strftime('%H:%M')}",
        f"价格：{_format_number(latest)}",
        f"规则置信度：{signal.confidence:.0%}",
    ]
    if review:
        lines.append(f"工程 AI：{_action_label(review.action)}，{review.confidence:.0%}")
    lines.extend(["", "工程 AI 结论：", review.summary if review else "工程 AI 复核暂不可用"])
    lines.extend(_section("工程 AI 理由", review.reasons if review else []))
    lines.extend(["", "Codex 二次判断：", _analysis_summary(codex_analysis)])
    lines.extend(_section("关键价位", _analysis_list(codex_analysis, "key_levels")))
    wait_for = _analysis_list(codex_analysis, "next_steps")
    if not wait_for and review:
        wait_for = review.wait_for
    lines.extend(_section("等待确认", wait_for))
    lines.extend(_section("失效条件", _analysis_list(codex_analysis, "invalidates")))
    risk_items = [*signal.risks]
    if review:
        risk_items.extend(review.risks)
    risk_items.extend(_analysis_list(codex_analysis, "risk_notes"))
    lines.extend(_section("风险", risk_items))
    execution_blockers = []
    if review:
        execution_blockers.extend(review.execution_blockers)
    execution_blockers.extend(_analysis_list(codex_analysis, "execution_blockers"))
    lines.extend(_section("执行阻断", execution_blockers))
    lines.extend(["", "提醒：仅供盘中辅助判断，不自动下单。"])
    return "\n".join(lines)


def format_review_day_feishu_message(point: ReplayPoint, quote: MarketQuote | None = None) -> str:
    action = point.llm_action or point.action
    name = quote.name if quote else point.symbol
    lines = [
        f"【T Maker 复核日通知】{name} {point.symbol}",
        "",
        f"信号：{_replay_action_label(point.action)}候选",
        f"时间：{point.timestamp[11:16]}",
        f"价格：{point.price:.2f}",
        f"规则置信度：{point.confidence:.0%}",
        f"工程 AI：{_replay_action_label(action)}，{_format_confidence(point.llm_confidence)}",
        "",
        "工程 AI 结论：",
        point.llm_summary or point.reason,
    ]
    lines.extend(_section("工程 AI 理由", point.llm_reasons))
    lines.extend(_section("等待确认", point.wait_for))
    lines.extend(_section("风险", point.risks))
    lines.extend(_section("执行阻断", point.execution_blockers or []))
    lines.extend(["", "提醒：历史复核通知，仅供复盘验证，不自动下单。"])
    return "\n".join(lines)


def _raise_for_feishu_error(response: httpx.Response) -> None:
    try:
        data = response.json()
    except ValueError:
        return
    if not isinstance(data, dict):
        return

    if "StatusCode" in data:
        status_code = data["StatusCode"]
        if status_code != 0:
            message = data.get("StatusMessage") or "Feishu delivery failed"
            raise FeishuDeliveryError(
                f"Feishu delivery failed: StatusCode={status_code}, message={message}"
            )
        return

    if "code" in data:
        code = data["code"]
        if code != 0:
            message = data.get("msg") or "Feishu delivery failed"
            raise FeishuDeliveryError(
                f"Feishu delivery failed: code={code}, message={message}"
            )


def _section(title: str, items: Iterable[str]) -> list[str]:
    unique_items = []
    for item in items:
        if item and item not in unique_items:
            unique_items.append(item)
    if not unique_items:
        return []
    return ["", f"{title}：", *[f"- {item}" for item in unique_items[:5]]]


def _analysis_summary(codex_analysis: dict[str, Any] | None) -> str:
    if not codex_analysis:
        return "Codex 二次分析暂不可用"
    summary = codex_analysis.get("summary")
    judgement = codex_analysis.get("judgement")
    if isinstance(summary, str) and summary:
        return f"{_judgement_label(judgement)}。{summary}" if judgement else summary
    return "Codex 二次分析暂不可用"


def _analysis_list(codex_analysis: dict[str, Any] | None, key: str) -> list[str]:
    if not codex_analysis:
        return []
    value = codex_analysis.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _kind_label(signal: Signal) -> str:
    if signal.kind.value == "candidate_buy":
        return "低吸候选"
    if signal.kind.value == "candidate_sell":
        return "高抛候选"
    if signal.kind.value == "suspected":
        return "疑似点"
    return "观望"


def _action_label(action: SignalAction) -> str:
    if action == SignalAction.BUY:
        return "低吸"
    if action == SignalAction.SELL:
        return "高抛"
    return "观望"


def _replay_action_label(action: str | SignalAction) -> str:
    if action == "buy":
        return "低吸"
    if action == "sell":
        return "高抛"
    return "观望"


def _judgement_label(value: object) -> str:
    if value == "buy":
        return "二次判断偏低吸"
    if value == "sell":
        return "二次判断偏高抛"
    if value == "avoid":
        return "二次判断偏回避"
    return "二次判断等待确认"


def _format_number(value: float | None) -> str:
    return f"{value:.2f}" if isinstance(value, float) else "--"


def _format_confidence(value: float | None) -> str:
    return f"{value:.0%}" if isinstance(value, float) else "--"
