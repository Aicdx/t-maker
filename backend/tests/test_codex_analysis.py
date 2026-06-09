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
