# Auto Monitor Feishu Codex Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add backend automatic A-share monitoring that reuses existing rule and AI review behavior, adds a Codex-style second analysis, and sends Feishu notifications for qualifying signals.

**Architecture:** Keep strategy and review logic in the existing snapshot path, then add small focused services around it: monitor configuration and policy, Feishu notification formatting/sending, Codex-style analysis, a monitor runner, and monitor API endpoints. The monitor should be testable synchronously and should not place orders or interact with broker software.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, httpx, pytest, existing OpenAI-compatible client style, existing Tencent market provider.

---

### Task 1: Monitor Settings, State, And Policy

**Files:**
- Modify: `backend/src/tmaker/config.py`
- Create: `backend/src/tmaker/monitor/__init__.py`
- Create: `backend/src/tmaker/monitor/state.py`
- Create: `backend/src/tmaker/monitor/policy.py`
- Test: `backend/tests/test_monitor_policy.py`

- [ ] **Step 1: Write failing policy tests**

Create `backend/tests/test_monitor_policy.py`:

```python
from datetime import datetime

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
```

- [ ] **Step 2: Run policy tests and verify they fail**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_monitor_policy.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'tmaker.monitor'`.

- [ ] **Step 3: Add monitor settings**

Modify `backend/src/tmaker/config.py` and add these fields to `Settings`:

```python
    monitor_auto_start: bool = False
    monitor_interval_seconds: float = 30
    monitor_min_ai_confidence: float = 0.6
    monitor_notify_hold: bool = False
    monitor_notify_suspected: bool = True
    monitor_dedup_window_minutes: int = 240
    codex_analysis_enabled: bool = True
    feishu_webhook_url: str = ""
    feishu_timeout_seconds: float = 8
```

- [ ] **Step 4: Create monitor package and state model**

Create `backend/src/tmaker/monitor/__init__.py`:

```python
"""Automatic monitoring services."""
```

Create `backend/src/tmaker/monitor/state.py`:

```python
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MonitorRuntimeState(BaseModel):
    running: bool = False
    last_tick_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_notified_signal_key: str | None = None
    notification_count: int = Field(default=0, ge=0)
```

- [ ] **Step 5: Implement monitor policy**

Create `backend/src/tmaker/monitor/policy.py`:

```python
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
```

- [ ] **Step 6: Run policy tests and verify they pass**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_monitor_policy.py -q
```

Expected: `8 passed`.

- [ ] **Step 7: Commit Task 1**

Run:

```powershell
cd D:\it\t-maker
git add backend/src/tmaker/config.py backend/src/tmaker/monitor/__init__.py backend/src/tmaker/monitor/state.py backend/src/tmaker/monitor/policy.py backend/tests/test_monitor_policy.py
git commit -m "feat: add monitor notification policy"
```

### Task 2: Feishu Notification Formatting And Sending

**Files:**
- Create: `backend/src/tmaker/notify/__init__.py`
- Create: `backend/src/tmaker/notify/feishu.py`
- Test: `backend/tests/test_feishu_notify.py`

- [ ] **Step 1: Write failing Feishu notifier tests**

Create `backend/tests/test_feishu_notify.py`:

```python
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
```

- [ ] **Step 2: Run notifier tests and verify they fail**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_feishu_notify.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'tmaker.notify'`.

- [ ] **Step 3: Create notifier package**

Create `backend/src/tmaker/notify/__init__.py`:

```python
"""Notification integrations."""
```

- [ ] **Step 4: Implement Feishu notifier**

Create `backend/src/tmaker/notify/feishu.py`:

```python
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from tmaker.domain.models import MarketQuote, Signal, SignalAction


class FeishuConfigError(RuntimeError):
    """Raised when Feishu notification is not configured."""


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
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = await client.post(self.webhook_url, json=payload)
            response.raise_for_status()


def format_feishu_message(
    *,
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
    lines.extend(_section("等待确认", _analysis_list(codex_analysis, "next_steps") or (review.wait_for if review else [])))
    lines.extend(_section("失效条件", _analysis_list(codex_analysis, "invalidates")))
    risk_items = [*signal.risks]
    if review:
        risk_items.extend(review.risks)
    risk_items.extend(_analysis_list(codex_analysis, "risk_notes"))
    lines.extend(_section("风险", risk_items))
    if review and review.execution_blockers:
        lines.extend(_section("执行阻断", review.execution_blockers))
    lines.extend(["", "提醒：仅供盘中辅助判断，不自动下单。"])
    return "\n".join(lines)


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
```

- [ ] **Step 5: Run notifier tests and verify they pass**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_feishu_notify.py -q
```

Expected: `4 passed`.

- [ ] **Step 6: Commit Task 2**

Run:

```powershell
cd D:\it\t-maker
git add backend/src/tmaker/notify/__init__.py backend/src/tmaker/notify/feishu.py backend/tests/test_feishu_notify.py
git commit -m "feat: add feishu notifier"
```

### Task 3: Codex-Style Second Analysis

**Files:**
- Create: `backend/src/tmaker/llm/codex_analysis.py`
- Test: `backend/tests/test_codex_analysis.py`

- [ ] **Step 1: Write failing Codex analysis tests**

Create `backend/tests/test_codex_analysis.py`:

```python
import json

import httpx
import pytest

from tmaker.llm.codex_analysis import CodexAnalysisClient, fallback_codex_analysis


@pytest.mark.asyncio
async def test_codex_analysis_client_calls_responses_api_with_schema() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "judgement": "buy",
                                        "summary": "更像观察型低吸，等待不破低点后再提高确认度。",
                                        "key_levels": ["支撑 121.80"],
                                        "next_steps": ["观察下一根 1 分钟 K 线"],
                                        "invalidates": ["跌破 121.80"],
                                        "risk_notes": ["高波动品种仓位要轻"],
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    client = CodexAnalysisClient(
        base_url="https://acid077.xin",
        api_key="test-key",
        model="gpt-5.5",
        wire_api="responses",
        transport=httpx.MockTransport(handler),
    )

    result = await client.create_analysis({"symbol": "300308"})

    body = seen["body"]
    assert seen["path"] == "/v1/responses"
    assert body["text"]["format"]["name"] == "t_codex_analysis"
    assert "盘中做 T 二次分析" in body["input"][0]["content"]
    assert result["judgement"] == "buy"
    assert result["summary"].startswith("更像观察型低吸")


@pytest.mark.asyncio
async def test_codex_analysis_client_supports_chat_completions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "judgement": "wait",
                                    "summary": "等待下一根确认。",
                                    "key_levels": [],
                                    "next_steps": ["不追第一根反弹"],
                                    "invalidates": ["放量跌破低点"],
                                    "risk_notes": ["AI 结论仅作辅助"],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = CodexAnalysisClient(
        base_url="https://acid077.xin",
        api_key="test-key",
        model="gpt-5.5",
        wire_api="chat_completions",
        transport=httpx.MockTransport(handler),
    )

    result = await client.create_analysis({"symbol": "300308"})

    assert result["judgement"] == "wait"
    assert result["next_steps"] == ["不追第一根反弹"]


def test_fallback_codex_analysis_mentions_unavailable() -> None:
    result = fallback_codex_analysis(RuntimeError("network"))

    assert result["judgement"] == "wait"
    assert "暂不可用" in result["summary"]
    assert "network" in result["risk_notes"][0]
```

- [ ] **Step 2: Run Codex analysis tests and verify they fail**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_codex_analysis.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'tmaker.llm.codex_analysis'`.

- [ ] **Step 3: Implement Codex analysis client**

Create `backend/src/tmaker/llm/codex_analysis.py`:

```python
from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, Field

from tmaker.llm.openai_client import _extract_responses_text


class CodexAnalysis(BaseModel):
    judgement: str
    summary: str
    key_levels: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    invalidates: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class CodexAnalysisClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 12,
        wire_api: str = "responses",
        reasoning_effort: str | None = None,
        disable_response_storage: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.wire_api = wire_api
        self.reasoning_effort = reasoning_effort
        self.disable_response_storage = disable_response_storage
        self.transport = transport

    async def create_analysis(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key or not self.model:
            raise RuntimeError("OpenAI-compatible API key or model is not configured")

        schema = _analysis_schema()
        if self.wire_api == "responses":
            payload = {
                "model": self.model,
                "input": [
                    {"role": "system", "content": _system_prompt()},
                    {
                        "role": "user",
                        "content": f"请做盘中做 T 二次分析：{json.dumps(context, ensure_ascii=False)}",
                    },
                ],
                "store": not self.disable_response_storage,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "t_codex_analysis",
                        "strict": True,
                        "schema": schema,
                    }
                },
            }
            if self.reasoning_effort:
                payload["reasoning"] = {"effort": self.reasoning_effort}
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/v1/responses",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                return CodexAnalysis.model_validate_json(
                    _extract_responses_text(response.json())
                ).model_dump()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": f"请做盘中做 T 二次分析：{json.dumps(context, ensure_ascii=False)}",
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "t_codex_analysis", "strict": True, "schema": schema},
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return CodexAnalysis.model_validate_json(content).model_dump()


def fallback_codex_analysis(exc: Exception) -> dict[str, Any]:
    message = str(exc).strip() or exc.__class__.__name__
    return {
        "judgement": "wait",
        "summary": "Codex 二次分析暂不可用，本条只包含工程 AI 结构化复核结论。",
        "key_levels": [],
        "next_steps": ["按工程 AI 的等待条件人工观察，不自动下单"],
        "invalidates": [],
        "risk_notes": [f"Codex 二次分析失败：{message[:180]}"],
    }


def _analysis_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "judgement": {"type": "string", "enum": ["buy", "sell", "wait", "avoid"]},
            "summary": {"type": "string"},
            "key_levels": {"type": "array", "items": {"type": "string"}},
            "next_steps": {"type": "array", "items": {"type": "string"}},
            "invalidates": {"type": "array", "items": {"type": "string"}},
            "risk_notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "judgement",
            "summary",
            "key_levels",
            "next_steps",
            "invalidates",
            "risk_notes",
        ],
        "additionalProperties": False,
    }


def _system_prompt() -> str:
    return (
        "你是 A 股盘中做 T 二次分析助手。你要基于工程 AI 结构化复核、"
        "分钟线、持仓和风险信息，给出简短但明确的盘中判断。不要承诺收益，"
        "不要建议自动下单。必须说明这是确认、观察、等待还是回避，并写出"
        "关键价位、下一步观察、失效条件和风险。只能输出符合 schema 的 JSON。"
    )
```

- [ ] **Step 4: Run Codex analysis tests and verify they pass**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_codex_analysis.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 3**

Run:

```powershell
cd D:\it\t-maker
git add backend/src/tmaker/llm/codex_analysis.py backend/tests/test_codex_analysis.py
git commit -m "feat: add codex-style signal analysis"
```

### Task 4: Monitor Runner And Deduplication

**Files:**
- Create: `backend/src/tmaker/monitor/runner.py`
- Test: `backend/tests/test_monitor_runner.py`

- [ ] **Step 1: Write failing monitor runner tests**

Create `backend/tests/test_monitor_runner.py`:

```python
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
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)

    assert snapshot.calls == 1
    assert len(analyzer.calls) == 1
    assert len(notifier.messages) == 1
    assert "Codex 二次判断" in notifier.messages[0]
    assert runner.state.notification_count == 1
    assert runner.state.last_error is None


@pytest.mark.asyncio
async def test_tick_deduplicates_repeated_signal() -> None:
    snapshot = FakeSnapshotService([_signal()])
    runner = MonitorRunner(
        snapshot_service=snapshot,
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(),
        policy=MonitorPolicy(min_ai_confidence=0.6),
    )

    await runner.tick(now=datetime(2026, 6, 9, 10, 24), force=True)
    await runner.tick(now=datetime(2026, 6, 9, 10, 25), force=True)

    assert runner.state.notification_count == 1


@pytest.mark.asyncio
async def test_tick_keeps_signal_retryable_when_feishu_fails() -> None:
    runner = MonitorRunner(
        snapshot_service=FakeSnapshotService([_signal()]),
        analyzer=FakeAnalyzer(),
        notifier=FakeNotifier(fail=True),
        policy=MonitorPolicy(min_ai_confidence=0.6),
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
    )

    await runner.tick(now=datetime(2026, 6, 9, 12, 0), force=False)

    assert snapshot.calls == 0
    assert runner.state.notification_count == 0
```

- [ ] **Step 2: Run runner tests and verify they fail**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_monitor_runner.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'tmaker.monitor.runner'`.

- [ ] **Step 3: Implement monitor runner**

Create `backend/src/tmaker/monitor/runner.py`:

```python
from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
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


class MonitorRunner:
    def __init__(
        self,
        *,
        snapshot_service: SnapshotService,
        analyzer: SignalAnalyzer,
        notifier: TextNotifier,
        policy: MonitorPolicy,
        interval_seconds: float = 30,
        dedup_window_minutes: int = 240,
    ) -> None:
        self.snapshot_service = snapshot_service
        self.analyzer = analyzer
        self.notifier = notifier
        self.policy = policy
        self.interval_seconds = interval_seconds
        self.dedup_window = timedelta(minutes=dedup_window_minutes)
        self.state = MonitorRuntimeState()
        self._task: asyncio.Task[None] | None = None
        self._notified_keys: dict[str, datetime] = {}

    async def start(self) -> None:
        if self._task and not self._task.done():
            self.state.running = True
            return
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
            payload = self.snapshot_service.refresh()
            await self._notify_payload(payload)
            self.state.last_success_at = current
            self.state.last_error = None
        except Exception as exc:
            self.state.last_error = str(exc).strip() or exc.__class__.__name__

    async def _run_loop(self) -> None:
        while self.state.running:
            await self.tick()
            await asyncio.sleep(self.interval_seconds)

    async def _notify_payload(self, payload: dict[str, Any]) -> None:
        signal_adapter = TypeAdapter(list[Signal])
        quote_adapter = TypeAdapter(dict[str, MarketQuote])
        signals = signal_adapter.validate_python(payload.get("signals", []))
        quotes = quote_adapter.validate_python(payload.get("quotes", {}))
        self._prune_dedup(datetime.now())
        for signal in signals:
            if not self.policy.should_notify(signal):
                continue
            key = signal_notification_key(signal)
            if key in self._notified_keys:
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
            self._notified_keys[key] = datetime.now()
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
    return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)
```

- [ ] **Step 4: Run runner tests and verify they pass**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_monitor_runner.py -q
```

Expected: `5 passed`.

- [ ] **Step 5: Commit Task 4**

Run:

```powershell
cd D:\it\t-maker
git add backend/src/tmaker/monitor/runner.py backend/tests/test_monitor_runner.py
git commit -m "feat: add monitor runner"
```

### Task 5: FastAPI Integration And Monitor Endpoints

**Files:**
- Modify: `backend/src/tmaker/api/app.py`
- Modify: `backend/src/tmaker/llm/codex_analysis.py`
- Test: `backend/tests/test_api.py`

- [ ] **Step 1: Write failing API tests**

Append these tests to `backend/tests/test_api.py`:

```python
def test_monitor_status_endpoint_reports_state() -> None:
    client = TestClient(create_app(minute_provider=FakeProvider(_provider_candles())))

    response = client.get("/api/monitor/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is False
    assert payload["notification_count"] == 0


def test_monitor_start_and_stop_are_idempotent() -> None:
    client = TestClient(create_app(minute_provider=FakeProvider(_provider_candles())))

    start_response = client.post("/api/monitor/start")
    second_start_response = client.post("/api/monitor/start")
    stop_response = client.post("/api/monitor/stop")
    second_stop_response = client.post("/api/monitor/stop")

    assert start_response.status_code == 200
    assert second_start_response.status_code == 200
    assert stop_response.status_code == 200
    assert second_stop_response.status_code == 200
    assert second_stop_response.json()["running"] is False


def test_monitor_test_feishu_reports_missing_webhook() -> None:
    client = TestClient(create_app(minute_provider=FakeProvider(_provider_candles())))

    response = client.post("/api/monitor/test-feishu")

    assert response.status_code == 503
    assert "FEISHU_WEBHOOK_URL" in response.json()["detail"]
```

- [ ] **Step 2: Run API tests and verify they fail**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_api.py -q
```

Expected: fail with 404 responses for `/api/monitor/*`.

- [ ] **Step 3: Add analyzer adapter method**

Modify `backend/src/tmaker/llm/codex_analysis.py` and add this class below `CodexAnalysisClient`:

```python
class CodexSignalAnalyzer:
    def __init__(self, client: CodexAnalysisClient, enabled: bool = True) -> None:
        self.client = client
        self.enabled = enabled

    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return fallback_codex_analysis(RuntimeError("CODEX_ANALYSIS_ENABLED is false"))
        return await self.client.create_analysis(context)
```

- [ ] **Step 4: Refactor snapshot refresh behind a service**

Modify `backend/src/tmaker/api/app.py`.

Add imports:

```python
from tmaker.llm.codex_analysis import CodexAnalysisClient, CodexSignalAnalyzer
from tmaker.monitor.policy import MonitorPolicy
from tmaker.monitor.runner import MonitorRunner
from tmaker.notify.feishu import FeishuConfigError, FeishuNotifier
```

Inside `create_app`, after `repo = ...`, add:

```python
    class SnapshotRefreshService:
        def refresh(self) -> dict:
            _refresh_from_provider(state, provider, reviewer)
            return _snapshot(state)
```

Then create monitor dependencies:

```python
    snapshot_service = SnapshotRefreshService()
    codex_client = CodexAnalysisClient(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout_seconds=settings.openai_timeout_seconds,
        wire_api=settings.openai_wire_api,
        reasoning_effort=settings.openai_reasoning_effort,
        disable_response_storage=settings.openai_disable_response_storage,
    )
    monitor_runner = MonitorRunner(
        snapshot_service=snapshot_service,
        analyzer=CodexSignalAnalyzer(codex_client, enabled=settings.codex_analysis_enabled),
        notifier=FeishuNotifier(
            webhook_url=settings.feishu_webhook_url,
            timeout_seconds=settings.feishu_timeout_seconds,
        ),
        policy=MonitorPolicy(
            min_ai_confidence=settings.monitor_min_ai_confidence,
            notify_hold=settings.monitor_notify_hold,
            notify_suspected=settings.monitor_notify_suspected,
        ),
        interval_seconds=settings.monitor_interval_seconds,
        dedup_window_minutes=settings.monitor_dedup_window_minutes,
    )
```

Modify `/api/snapshot` to use the service:

```python
    @app.get("/api/snapshot")
    def snapshot() -> dict:
        return snapshot_service.refresh()
```

- [ ] **Step 5: Add monitor endpoints and lifecycle hooks**

Still inside `create_app`, add these routes:

```python
    @app.on_event("startup")
    async def start_monitor_when_enabled() -> None:
        if settings.monitor_auto_start:
            await monitor_runner.start()

    @app.on_event("shutdown")
    async def stop_monitor_on_shutdown() -> None:
        await monitor_runner.stop()

    @app.get("/api/monitor/status")
    def monitor_status() -> dict:
        return monitor_runner.state.model_dump(mode="json")

    @app.post("/api/monitor/start")
    async def monitor_start() -> dict:
        await monitor_runner.start()
        return monitor_runner.state.model_dump(mode="json")

    @app.post("/api/monitor/stop")
    async def monitor_stop() -> dict:
        await monitor_runner.stop()
        return monitor_runner.state.model_dump(mode="json")

    @app.post("/api/monitor/test-feishu")
    async def monitor_test_feishu() -> dict:
        try:
            await monitor_runner.notifier.send_text(
                "【T Maker 测试通知】飞书机器人已连通。\n提醒：仅供盘中辅助判断，不自动下单。"
            )
        except FeishuConfigError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=_format_provider_error(exc)) from exc
        return {"status": "ok"}
```

- [ ] **Step 6: Run API tests and fix FastAPI lifecycle warnings only if they fail tests**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_api.py -q
```

Expected: existing tests plus new monitor tests pass.

- [ ] **Step 7: Commit Task 5**

Run:

```powershell
cd D:\it\t-maker
git add backend/src/tmaker/api/app.py backend/src/tmaker/llm/codex_analysis.py backend/tests/test_api.py
git commit -m "feat: expose monitor api"
```

### Task 6: Full Backend Verification And Documentation Touch-Up

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

- [ ] **Step 1: Add monitor environment example**

Modify `.env.example` and add:

```env
MONITOR_AUTO_START=false
MONITOR_INTERVAL_SECONDS=30
MONITOR_MIN_AI_CONFIDENCE=0.60
MONITOR_NOTIFY_HOLD=false
MONITOR_NOTIFY_SUSPECTED=true
CODEX_ANALYSIS_ENABLED=true
FEISHU_WEBHOOK_URL=
```

- [ ] **Step 2: Add README monitor section**

Modify `README.md` and add this section after the configuration section:

~~~markdown
## 自动盯盘与飞书通知

后端支持自动盯盘。启用后，交易时间内会按 `MONITOR_INTERVAL_SECONDS` 轮询实时行情，复用现有规则与 AI 复核逻辑，并在 AI 确认低吸/高抛信号时发送飞书通知。通知包含工程 AI 结构化复核和 Codex 风格二次分析。

默认不会自动启动，避免开发环境误发通知。配置 `.env`：

```env
MONITOR_AUTO_START=true
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/...
```

测试飞书：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/monitor/test-feishu
```

查看状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/monitor/status
```

提醒内容仅供盘中辅助判断，不自动下单。
~~~

- [ ] **Step 3: Run targeted backend tests**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest tests/test_monitor_policy.py tests/test_feishu_notify.py tests/test_codex_analysis.py tests/test_monitor_runner.py tests/test_api.py -q
```

Expected: all selected tests pass.

- [ ] **Step 4: Run full backend tests**

Run:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all backend tests pass.

- [ ] **Step 5: Commit Task 6**

Run:

```powershell
cd D:\it\t-maker
git add README.md .env.example
git commit -m "docs: document auto monitor setup"
```

## Final Verification

- [ ] Run backend full test suite:

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest -q
```

- [ ] Verify the working tree only contains intentional changes:

```powershell
cd D:\it\t-maker
git status --short
```

- [ ] Optional manual Feishu smoke test after configuring `.env`:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/monitor/test-feishu
```

Expected: Feishu receives a `T Maker 测试通知` message.
