"""Anthropic Claude 适配器."""
from typing import AsyncGenerator, List, Optional
from react_ai.llm.base import BaseLLMClient, LLMResponse
from react_ai.schemas.messages import ChatMessage, UsageStats
from react_ai.schemas.tools import ToolDefinition


class AnthropicProvider(BaseLLMClient):
    def __init__(self, api_key: Optional[str] = None, model: str = "claude-3-5-sonnet-20241022"):
        self.api_key = api_key
        self.model = model

    async def generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        return LLMResponse(
            content=f"[Anthropic {self.model}]: Claude 推理完成。",
            tool_calls=None,
            usage=UsageStats(prompt_tokens=120, completion_tokens=45, total_tokens=165),
        )

    async def stream_generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncGenerator[str, None]:
        yield f"[Anthropic {self.model} stream chunk]"
