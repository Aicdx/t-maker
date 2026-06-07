import json

import httpx
import pytest

from tmaker.llm.openai_client import OpenAICompatibleClient


@pytest.mark.asyncio
async def test_openai_client_calls_responses_api_with_json_schema_and_store_disabled() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
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
                                        "action": "buy",
                                        "confidence": 0.61,
                                        "summary": "候选点可观察",
                                        "reasons": ["规则共振"],
                                        "risks": ["继续下探"],
                                        "wait_for": ["下一根 K 线不破低点"],
                                        "execution_allowed": False,
                                        "execution_blockers": ["可用资金不足"],
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    client = OpenAICompatibleClient(
        base_url="https://acid077.xin",
        api_key="test-key",
        model="gpt-5.5",
        wire_api="responses",
        reasoning_effort="xhigh",
        disable_response_storage=True,
        transport=httpx.MockTransport(handler),
    )

    review = await client.create_review({"symbol": "600000"})

    body = seen["body"]
    assert seen["path"] == "/v1/responses"
    assert seen["auth"] == "Bearer test-key"
    assert body["model"] == "gpt-5.5"
    assert body["store"] is False
    assert body["reasoning"]["effort"] == "xhigh"
    assert body["text"]["format"]["type"] == "json_schema"
    assert "execution_allowed" in body["text"]["format"]["schema"]["required"]
    assert "execution_blockers" in body["text"]["format"]["schema"]["required"]
    assert "市场动作" in body["input"][0]["content"]
    assert "分批主动高抛" in body["input"][0]["content"]
    assert review["summary"] == "候选点可观察"
