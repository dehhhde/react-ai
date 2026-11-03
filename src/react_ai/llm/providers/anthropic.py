"""Anthropic Claude 适配器：真实 Messages API 调用与工具调用协议转换.

协议映射（统一消息 ↔ Anthropic Messages API）：
- SYSTEM 消息 → 顶层 system 参数；
- ASSISTANT.tool_calls → assistant 消息中的 tool_use 内容块；
- TOOL 消息 → user 消息中的 tool_result 内容块（连续工具结果合并为同一条 user 消息）；
- tool_choice：auto/none 直映射，required → {"type":"any"}，工具名 → {"type":"tool"}；
- 流式：content_block_delta 的 text_delta 逐段产出，tool_use 的 input_json_delta
  按 block index 增量拼接参数，message_stop 后给出完整 tool_calls。
"""
import os
import json
import logging
import httpx
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from react_ai.llm.base import BaseLLMClient, LLMResponse, LLMStreamChunk
from react_ai.schemas.messages import ChatMessage, MessageRole, ToolCall, UsageStats
from react_ai.schemas.tools import ToolDefinition

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
# Anthropic API 要求必须显式提供 max_tokens，未指定时使用该默认值
DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider(BaseLLMClient):
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        timeout_seconds: float = 60.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.api_base = (api_base or os.getenv("ANTHROPIC_API_BASE") or "https://api.anthropic.com").rstrip("/")
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
        if self.api_key:
            return await self._call_real_api(messages, tools, temperature, max_tokens, tool_choice)
        return self._simulate_react_step(messages, tools)

    async def _call_real_api(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        url = f"{self.api_base}/v1/messages"
        payload = self._build_payload(messages, tools, temperature, max_tokens, tool_choice)

        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()

        return self._parse_completion(data)

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]],
        temperature: float,
        max_tokens: Optional[int],
        tool_choice: Optional[str],
        stream: bool = False,
    ) -> Dict[str, Any]:
        system, formatted = self._format_messages(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": formatted,
            "temperature": temperature,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
        }
        if system:
            payload["system"] = system
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters.model_dump(),
                }
                for t in tools
            ]
            if tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
            elif tool_choice in ("auto", "none"):
                payload["tool_choice"] = {"type": tool_choice}
            elif tool_choice:
                payload["tool_choice"] = {"type": "tool", "name": tool_choice}
        return payload

    @staticmethod
    def _append_block(formatted: List[Dict[str, Any]], role: str, block: Dict[str, Any]) -> None:
        """同角色连续消息合并为单条多内容块消息（Anthropic 要求角色交替、tool_result 归入 user）."""
        if formatted and formatted[-1]["role"] == role:
            formatted[-1]["content"].append(block)
        else:
            formatted.append({"role": role, "content": [block]})

    def _format_messages(self, messages: List[ChatMessage]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        system_parts: List[str] = []
        formatted: List[Dict[str, Any]] = []

        for m in messages:
            if m.role == MessageRole.SYSTEM:
                if m.content:
                    system_parts.append(m.content)
            elif m.role == MessageRole.TOOL:
                self._append_block(formatted, "user", {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content or "",
                })
            elif m.role == MessageRole.ASSISTANT:
                blocks: List[Dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls or []:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
                for b in blocks:
                    self._append_block(formatted, "assistant", b)
            else:  # USER
                if m.content:
                    self._append_block(formatted, "user", {"type": "text", "text": m.content})

        return ("\n\n".join(system_parts) or None), formatted

    @staticmethod
    def _parse_completion(data: Dict[str, Any]) -> LLMResponse:
        texts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {})
                )

        usage_data = data.get("usage", {})
        prompt = usage_data.get("input_tokens", 0)
        completion = usage_data.get("output_tokens", 0)
        return LLMResponse(
            content="".join(texts) or None,
            tool_calls=tool_calls or None,
            usage=UsageStats(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
            ),
            raw_response=data,
        )

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

        url = f"{self.api_base}/v1/messages"
        payload = self._build_payload(messages, tools, temperature, max_tokens, tool_choice, stream=True)
        usage = UsageStats()
        partial: Dict[int, Dict[str, str]] = {}

        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            async with client.stream("POST", url, headers=self._headers(), json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")
                    if etype == "message_start":
                        usage.prompt_tokens = (event.get("message") or {}).get("usage", {}).get("input_tokens", 0)
                    elif etype == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            partial[event.get("index", 0)] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": "",
                            }
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield LLMStreamChunk(delta=delta["text"])
                        elif delta.get("type") == "input_json_delta":
                            slot = partial.get(event.get("index", 0))
                            if slot is not None:
                                slot["arguments"] += delta.get("partial_json", "")
                    elif etype == "message_delta":
                        output = (event.get("usage") or {}).get("output_tokens")
                        if output is not None:
                            usage.completion_tokens = output

        tool_calls: Optional[List[ToolCall]] = None
        if partial:
            tool_calls = []
            for idx in sorted(partial):
                slot = partial[idx]
                raw = slot["arguments"]
                try:
                    arguments = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    logger.warning("anthropic_tool_call_invalid_json tool=%s raw=%s", slot["name"], raw[:200])
                    arguments = {"_raw_arguments": raw, "_parse_error": True}
                tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=arguments))

        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        yield LLMStreamChunk(tool_calls=tool_calls, usage=usage)

    async def stream_generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncGenerator[str, None]:
        async for chunk in self.stream_generate_full(messages, tools, temperature):
            if chunk.delta:
                yield chunk.delta
