"""DeepSeek 大模型提供商适配器 (原生兼容 OpenAI 协议与 Function Calling)."""
import os
import httpx
from typing import AsyncGenerator, List, Optional
from react_ai.llm.base import BaseLLMClient, LLMResponse, LLMStreamChunk
from react_ai.schemas.messages import ChatMessage
from react_ai.schemas.tools import ToolDefinition
from react_ai.llm.providers.openai import (
    build_openai_payload,
    parse_openai_completion,
    stream_openai_sse,
)


class DeepSeekProvider(BaseLLMClient):
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        model: str = "deepseek-chat",
        timeout_seconds: float = 60.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY")
        self.api_base = (api_base or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com").rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        # 测试注入点：可传入 httpx.MockTransport 隔离真实网络
        self._transport = transport

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        url = f"{self.api_base}/chat/completions"
        payload = build_openai_payload(self.model, messages, tools, temperature, max_tokens, tool_choice)

        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
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
        url = f"{self.api_base}/chat/completions"
        payload = build_openai_payload(
            self.model, messages, tools, temperature, max_tokens, tool_choice, stream=True
        )

        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            async for chunk in stream_openai_sse(client, url, self._headers(), payload):
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
