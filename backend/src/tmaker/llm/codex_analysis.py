from __future__ import annotations

import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from tmaker.llm.openai_client import _extract_responses_text


class CodexAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judgement: Literal["buy", "sell", "wait", "avoid"]
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
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
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
                "json_schema": {
                    "name": "t_codex_analysis",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
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
