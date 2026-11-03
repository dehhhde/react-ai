"""OpenAI 兼容协议适配器 (支持真实 HTTP 请求与零 Key 离线演示模拟).

本模块同时提供 OpenAI 兼容协议的共享助手函数（消息格式化、工具 Schema 转换、
tool_calls 容错解析、SSE 流式增量拼接），供 DeepSeek 等兼容厂商复用。
"""
import os
import json
import logging
import httpx
from typing import Any, AsyncGenerator, Dict, List, Optional
from react_ai.llm.base import BaseLLMClient, LLMResponse, LLMStreamChunk
from react_ai.schemas.messages import ChatMessage, ToolCall, UsageStats
from react_ai.schemas.tools import ToolDefinition

logger = logging.getLogger(__name__)


def format_openai_messages(messages: List[ChatMessage]) -> List[Dict[str, Any]]:
    """统一消息 → OpenAI chat/completions 消息协议（含 tool_calls / tool_call_id）."""
    formatted: List[Dict[str, Any]] = []
    for m in messages:
        msg_dict: Dict[str, Any] = {"role": m.role.value, "content": m.content or ""}
        if m.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                }
                for tc in m.tool_calls
            ]
        if m.tool_call_id:
            msg_dict["tool_call_id"] = m.tool_call_id
        formatted.append(msg_dict)
    return formatted


def _strict_parameters(params: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI strict 模式要求：additionalProperties=false 且所有 properties 均在 required 中."""
    properties = params.get("properties", {}) or {}
    out = dict(params)
    out["additionalProperties"] = False
    if properties:
        out["required"] = list(properties.keys())
    return out


def build_openai_tools_payload(tools: List[ToolDefinition]) -> List[Dict[str, Any]]:
    """工具定义 → OpenAI tools 数组；metadata.strict=True 时启用 structured outputs."""
    payload_tools = []
    for t in tools:
        parameters = t.parameters.model_dump()
        function: Dict[str, Any] = {
            "name": t.name,
            "description": t.description,
            "parameters": parameters,
        }
        if t.metadata.get("strict"):
            function["parameters"] = _strict_parameters(parameters)
            function["strict"] = True
        payload_tools.append({"type": "function", "function": function})
    return payload_tools


def build_openai_payload(
    model: str,
    messages: List[ChatMessage],
    tools: Optional[List[ToolDefinition]] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    tool_choice: Optional[str] = None,
    stream: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": format_openai_messages(messages),
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = build_openai_tools_payload(tools)
        # tool_choice 完整语义：None（不发送）、"auto"、"none"、"required"、或指定工具名（强制调用）
        if tool_choice in ("auto", "none", "required"):
            payload["tool_choice"] = tool_choice
        elif tool_choice:
            payload["tool_choice"] = {"type": "function", "function": {"name": tool_choice}}
    return payload


def parse_openai_arguments(name: Optional[str], raw_args: Any) -> Dict[str, Any]:
    """模型可能输出截断的非法 JSON，容错解析：失败时用原始字符串并标记，避免终止 run."""
    if isinstance(raw_args, dict):
        return raw_args
    raw = raw_args or ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw_arguments": raw, "_parse_error": True}
    except json.JSONDecodeError:
        logger.warning("openai_tool_call_invalid_json tool=%s raw=%s", name, raw[:200])
        return {"_raw_arguments": raw, "_parse_error": True}


def parse_openai_completion(data: Dict[str, Any]) -> LLMResponse:
    """非流式 chat/completions 响应 → 统一 LLMResponse."""
    choice = data["choices"][0]["message"]
    tool_calls: Optional[List[ToolCall]] = None
    if choice.get("tool_calls"):
        tool_calls = [
            ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=parse_openai_arguments(tc["function"].get("name"), tc["function"].get("arguments")),
            )
            for tc in choice["tool_calls"]
        ]

    usage_data = data.get("usage", {})
    usage = UsageStats(
        prompt_tokens=usage_data.get("prompt_tokens", 0),
        completion_tokens=usage_data.get("completion_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
    )

    return LLMResponse(
        content=choice.get("content"),
        tool_calls=tool_calls,
        usage=usage,
        raw_response=data,
    )


def _assemble_streamed_tool_calls(partial: Dict[int, Dict[str, str]]) -> Optional[List[ToolCall]]:
    """把按 index 分片的流式 tool_call 增量拼接为完整 ToolCall 列表."""
    if not partial:
        return None
    calls = []
    for idx in sorted(partial):
        slot = partial[idx]
        calls.append(
            ToolCall(
                id=slot["id"],
                name=slot["name"],
                arguments=parse_openai_arguments(slot["name"], slot["arguments"]),
            )
        )
    return calls


async def stream_openai_sse(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
) -> AsyncGenerator[LLMStreamChunk, None]:
    """消费 OpenAI 兼容 SSE 流：逐段产出文本增量；tool_calls 增量参数按 index 拼接，
    流结束后在最后一个 chunk 上给出完整 tool_calls 与 usage."""
    partial: Dict[int, Dict[str, str]] = {}
    usage: Optional[UsageStats] = None

    async with client.stream("POST", url, headers=headers, json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            usage_data = chunk.get("usage")
            if usage_data:
                usage = UsageStats(
                    prompt_tokens=usage_data.get("prompt_tokens", 0),
                    completion_tokens=usage_data.get("completion_tokens", 0),
                    total_tokens=usage_data.get("total_tokens", 0),
                )

            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            content = delta.get("content")
            if content:
                yield LLMStreamChunk(delta=content)

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = partial.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    yield LLMStreamChunk(tool_calls=_assemble_streamed_tool_calls(partial), usage=usage)


class OpenAIProvider(BaseLLMClient):
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        model: str = "gpt-4o",
        timeout_seconds: float = 60.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.api_base = (api_base or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        # 测试注入点：可传入 httpx.MockTransport 隔离真实网络
        self._transport = transport

    async def generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        # 1. 若配置了 API Key，发起真实的 HTTP 接口调用
        if self.api_key:
            return await self._call_real_api(messages, tools, temperature, max_tokens, tool_choice)

        # 2. 离线无 Key 模式：提供逼真的 ReAct 多步推理与工具调用演示
        return self._simulate_react_step(messages, tools)

    async def _call_real_api(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = build_openai_payload(self.model, messages, tools, temperature, max_tokens, tool_choice)

        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        return parse_openai_completion(data)

    async def stream_generate_full(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        if not self.api_key:
            async for chunk in super().stream_generate_full(messages, tools, temperature, max_tokens, tool_choice):
                yield chunk
            return

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = build_openai_payload(
            self.model, messages, tools, temperature, max_tokens, tool_choice, stream=True
        )

        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            async for chunk in stream_openai_sse(client, url, headers, payload):
                yield chunk

    async def stream_generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncGenerator[str, None]:
        async for chunk in self.stream_generate_full(messages, tools, temperature):
            if chunk.delta:
                yield chunk.delta
