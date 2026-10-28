"""统一大模型接口规范与响应实体."""
from abc import ABC, abstractmethod
from typing import AsyncGenerator, List, Optional
from pydantic import BaseModel, Field
from react_ai.schemas.messages import ChatMessage, ToolCall, UsageStats
from react_ai.schemas.tools import ToolDefinition


class LLMResponse(BaseModel):
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    usage: UsageStats = Field(default_factory=UsageStats)
    raw_response: Optional[dict] = None


class BaseLLMClient(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """发起单次模型推理请求."""
        pass

    @abstractmethod
    async def stream_generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncGenerator[str, None]:
        """发起流式文本输出推理."""
        pass
