"""2.10 协议与模型适配单测：流式 tool call 增量拼接、tool_choice 完整语义、
strict/structured outputs、Anthropic 真实工具调用协议转换与多供应商流式容灾."""
import json
from typing import AsyncGenerator, List, Optional

import httpx
import pytest

from react_ai.llm.base import BaseLLMClient, LLMResponse, LLMStreamChunk
from react_ai.llm.providers.anthropic import AnthropicProvider, DEFAULT_MAX_TOKENS
from react_ai.llm.providers.deepseek import DeepSeekProvider
from react_ai.llm.providers.openai import OpenAIProvider
from react_ai.llm.router import LLMRouter
from react_ai.schemas.messages import ChatMessage, MessageRole, ToolCall, UsageStats
from react_ai.schemas.tools import ToolDefinition, ToolParameterSchema


def make_tool(name: str = "calculate", strict: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="计算数学表达式",
        parameters=ToolParameterSchema(
            type="object",
            properties={"expression": {"type": "string"}, "precision": {"type": "integer"}},
            required=["expression"],
        ),
        metadata={"strict": True} if strict else {},
    )


def user_msgs() -> List[ChatMessage]:
    return [ChatMessage(role=MessageRole.USER, content="算一下 (35 * 12) + 180")]


def sse_body(objects, done: bool = True) -> str:
    lines = ["data: " + json.dumps(o, ensure_ascii=False) for o in objects]
    if done:
        lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def completion_json(tool_calls=None, content="ok") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"index": 0, "message": message}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


# ---------------------------------------------------------------------------
# OpenAI：流式 tool call 增量拼接
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_stream_assembles_fragmented_tool_calls():
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "我来"}}]},
        {"choices": [{"index": 0, "delta": {"content": "算一下"}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_9", "function": {"name": "calculate", "arguments": '{"ex'}}
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'pression": "(35 * 12) + 180"}'}}
                        ]
                    },
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}},
    ]
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, text=sse_body(chunks))

    provider = OpenAIProvider(api_key="k", transport=httpx.MockTransport(handler))
    collected = [c async for c in provider.stream_generate_full(user_msgs(), [make_tool()])]

    assert captured["payload"]["stream"] is True
    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert "".join(c.delta for c in collected) == "我来算一下"

    final = collected[-1]
    assert final.tool_calls == [
        ToolCall(id="call_9", name="calculate", arguments={"expression": "(35 * 12) + 180"})
    ]
    assert final.usage.total_tokens == 16


@pytest.mark.asyncio
async def test_openai_stream_generate_yields_text_only():
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "你好"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "calculate", "arguments": "{}"}}]}}]},
    ]
    provider = OpenAIProvider(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse_body(chunks))))
    texts = [t async for t in provider.stream_generate(user_msgs(), [make_tool()])]
    assert texts == ["你好"]


@pytest.mark.asyncio
async def test_openai_stream_invalid_json_args_degrades_not_raises():
    chunks = [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "calculate", "arguments": '{"expression": "trunc'}}]}}]},
    ]
    provider = OpenAIProvider(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse_body(chunks))))
    collected = [c async for c in provider.stream_generate_full(user_msgs(), [make_tool()])]
    args = collected[-1].tool_calls[0].arguments
    assert args["_parse_error"] is True
    assert args["_raw_arguments"] == '{"expression": "trunc'


# ---------------------------------------------------------------------------
# OpenAI：strict structured outputs 与 tool_choice 完整语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_strict_schema_and_tool_choice_semantics():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=completion_json())

    provider = OpenAIProvider(api_key="k", transport=httpx.MockTransport(handler))

    await provider.generate(user_msgs(), [make_tool(strict=True)], tool_choice="required")
    payload = captured[0]
    function = payload["tools"][0]["function"]
    assert function["strict"] is True
    assert function["parameters"]["additionalProperties"] is False
    assert set(function["parameters"]["required"]) == {"expression", "precision"}
    assert payload["tool_choice"] == "required"

    await provider.generate(user_msgs(), [make_tool()], tool_choice="none")
    assert captured[1]["tool_choice"] == "none"
    assert "strict" not in captured[1]["tools"][0]["function"]

    await provider.generate(user_msgs(), [make_tool()], tool_choice="calculate")
    assert captured[2]["tool_choice"] == {"type": "function", "function": {"name": "calculate"}}


# ---------------------------------------------------------------------------
# DeepSeek：OpenAI 兼容协议语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deepseek_tool_choice_passthrough_and_streaming_tool_calls():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if captured[-1].get("stream"):
            chunks = [
                {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_d", "function": {"name": "calculate", "arguments": '{"expression": "1+1"}'}}]}}]},
            ]
            return httpx.Response(200, text=sse_body(chunks))
        return httpx.Response(200, json=completion_json())

    provider = DeepSeekProvider(api_key="k", transport=httpx.MockTransport(handler))

    await provider.generate(user_msgs(), [make_tool()], tool_choice="auto")
    assert captured[0]["tool_choice"] == "auto"

    await provider.generate(user_msgs(), [make_tool()], tool_choice="calculate")
    assert captured[1]["tool_choice"] == {"type": "function", "function": {"name": "calculate"}}

    collected = [c async for c in provider.stream_generate_full(user_msgs(), [make_tool()])]
    assert collected[-1].tool_calls == [ToolCall(id="call_d", name="calculate", arguments={"expression": "1+1"})]


# ---------------------------------------------------------------------------
# Anthropic：真实工具调用协议转换
# ---------------------------------------------------------------------------


def anthropic_tool_roundtrip_messages() -> List[ChatMessage]:
    return [
        ChatMessage(role=MessageRole.SYSTEM, content="你是售后助手"),
        ChatMessage(role=MessageRole.USER, content="查一下我的历史订单"),
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="先查询历史记录",
            tool_calls=[ToolCall(id="tu_1", name="query_history", arguments={"user_id": "u1"})],
        ),
        ChatMessage(role=MessageRole.TOOL, content='{"orders": 3}', tool_call_id="tu_1"),
    ]


@pytest.mark.asyncio
async def test_anthropic_generate_converts_tool_protocol():
    captured = {}

    response_body = {
        "content": [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "id": "tu_2", "name": "refund", "input": {"amount": 10}},
        ],
        "usage": {"input_tokens": 7, "output_tokens": 9},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=response_body)

    provider = AnthropicProvider(api_key="sk-ant-test", transport=httpx.MockTransport(handler))
    res = await provider.generate(
        anthropic_tool_roundtrip_messages(), [make_tool("refund")], tool_choice="refund"
    )

    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"

    payload = captured["payload"]
    assert payload["system"] == "你是售后助手"
    assert payload["max_tokens"] == DEFAULT_MAX_TOKENS
    assert payload["tool_choice"] == {"type": "tool", "name": "refund"}
    assert payload["tools"][0]["input_schema"]["type"] == "object"

    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "user"]
    assistant_blocks = payload["messages"][1]["content"]
    assert assistant_blocks[0] == {"type": "text", "text": "先查询历史记录"}
    assert assistant_blocks[1] == {
        "type": "tool_use",
        "id": "tu_1",
        "name": "query_history",
        "input": {"user_id": "u1"},
    }
    tool_result = payload["messages"][2]["content"][0]
    assert tool_result == {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"orders": 3}'}

    assert res.content == "好的"
    assert res.tool_calls == [ToolCall(id="tu_2", name="refund", arguments={"amount": 10})]
    assert res.usage.total_tokens == 16


@pytest.mark.asyncio
async def test_anthropic_tool_choice_mapping():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    for choice, expected in [
        ("required", {"type": "any"}),
        ("auto", {"type": "auto"}),
        ("none", {"type": "none"}),
    ]:
        await provider.generate(user_msgs(), [make_tool()], tool_choice=choice)
        assert captured[-1]["tool_choice"] == expected


@pytest.mark.asyncio
async def test_anthropic_stream_assembles_input_json_delta():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "正在计算"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_3", "name": "calculate"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"expr'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'ession": 7}'}},
        {"type": "message_delta", "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]
    body = "\n\n".join("event: x\ndata: " + json.dumps(e, ensure_ascii=False) for e in events) + "\n\n"

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    collected = [c async for c in provider.stream_generate_full(user_msgs(), [make_tool()])]

    assert "".join(c.delta for c in collected) == "正在计算"
    final = collected[-1]
    assert final.tool_calls == [ToolCall(id="tu_3", name="calculate", arguments={"expression": 7})]
    assert final.usage.prompt_tokens == 5
    assert final.usage.completion_tokens == 3
    assert final.usage.total_tokens == 8


@pytest.mark.asyncio
async def test_anthropic_offline_mode_simulates_tool_call(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider()
    res = provider._simulate_react_step(user_msgs(), [make_tool()])
    assert res.tool_calls and res.tool_calls[0].name == "calculate"

    res2 = await provider.generate(user_msgs(), [make_tool()])
    assert res2.tool_calls and res2.tool_calls[0].arguments == {"expression": "(35 * 12) + 180"}


# ---------------------------------------------------------------------------
# 多供应商流式容灾
# ---------------------------------------------------------------------------


class _DownClient(BaseLLMClient):
    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None, tool_choice=None):
        raise RuntimeError("provider down")

    async def stream_generate(self, messages, tools=None, temperature=0.2):
        raise RuntimeError("provider down")
        yield ""  # pragma: no cover


class _BackupClient(BaseLLMClient):
    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None, tool_choice=None):
        return LLMResponse(
            content="备节点接管",
            tool_calls=[ToolCall(id="b1", name="calculate", arguments={"expression": "1+1"})],
            usage=UsageStats(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        )

    async def stream_generate(self, messages, tools=None, temperature=0.2):
        yield "备节点接管"  # pragma: no cover


@pytest.mark.asyncio
async def test_router_stream_generate_full_falls_back_with_tool_calls():
    router = LLMRouter(primary=_DownClient(), fallbacks=[_BackupClient()])
    collected = [c async for c in router.stream_generate_full(user_msgs(), [make_tool()])]
    assert collected[-1].tool_calls == [ToolCall(id="b1", name="calculate", arguments={"expression": "1+1"})]
    assert "".join(c.delta for c in collected) == "备节点接管"
