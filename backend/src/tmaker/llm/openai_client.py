from __future__ import annotations

import json

import httpx


class OpenAICompatibleClient:
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

    async def create_review(self, context: dict) -> dict:
        if not self.api_key or not self.model:
            raise RuntimeError("OpenAI-compatible API key or model is not configured")

        schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "summary": {"type": "string"},
                "reasons": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "wait_for": {"type": "array", "items": {"type": "string"}},
                "execution_allowed": {"type": "boolean"},
                "execution_blockers": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "action",
                "confidence",
                "summary",
                "reasons",
                "risks",
                "wait_for",
                "execution_allowed",
                "execution_blockers",
            ],
            "additionalProperties": False,
        }
        if self.wire_api == "responses":
            return await self._create_responses_review(context, schema)

        return await self._create_chat_completions_review(context, schema)

    async def _create_responses_review(self, context: dict, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "你是 A 股盘中做 T 市场信号复核助手，只能输出符合 schema 的 JSON，"
                        "不提供确定性收益承诺。action 表示市场动作：buy=低吸点、sell=高抛点、"
                        "hold=市场条件不足。资金、底仓、整数手、T+1限制只能影响 "
                        "execution_allowed 和 execution_blockers，不得单独因为账户不可执行而把"
                        "有效市场信号改成 hold。强趋势冲高、日内涨幅较大且明显高于全天均价时，"
                        "允许判断为分批主动高抛，不必等待跌破或破位确认；回落确认只作为提高"
                        "信心的因素。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"请复核这个 A 股做 T 候选信号：{json.dumps(context, ensure_ascii=False)}",
                },
            ],
            "store": not self.disable_response_storage,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "t_signal_review",
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
            return json.loads(_extract_responses_text(response.json()))

    async def _create_chat_completions_review(self, context: dict, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 A 股盘中做 T 市场信号复核助手，只能输出符合 schema 的 JSON，"
                        "不提供确定性收益承诺。action 表示市场动作：buy=低吸点、sell=高抛点、"
                        "hold=市场条件不足。资金、底仓、整数手、T+1限制只能影响 "
                        "execution_allowed 和 execution_blockers，不得单独因为账户不可执行而把"
                        "有效市场信号改成 hold。强趋势冲高、日内涨幅较大且明显高于全天均价时，"
                        "允许判断为分批主动高抛，不必等待跌破或破位确认；回落确认只作为提高"
                        "信心的因素。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"请复核这个 A 股做 T 候选信号：{json.dumps(context, ensure_ascii=False)}",
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "t_signal_review", "strict": True, "schema": schema},
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return httpx.Response(200, content=content).json()


def _extract_responses_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                return text

    raise ValueError("Responses payload does not contain output text")
