"""企业级多模型智能路由与容灾降级网关."""
from typing import AsyncGenerator, Dict, List, Optional
from react_ai.llm.base import BaseLLMClient, LLMResponse
from react_ai.schemas.messages import ChatMessage
from react_ai.schemas.tools import ToolDefinition


class LLMRouter(BaseLLMClient):
    def __init__(self, primary: BaseLLMClient, fallbacks: Optional[List[BaseLLMClient]] = None):
        self.primary = primary
        self.fallbacks = fallbacks or []

    async def generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        clients = [self.primary] + self.fallbacks
        last_error = None

        for client in clients:
            try:
                return await client.generate(messages, tools, temperature, max_tokens, tool_choice)
            except Exception as err:
                last_error = err
                continue

        raise RuntimeError(f"全部 LLM 节点推理均失败，末次异常: {last_error}")

    async def stream_generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncGenerator[str, None]:
        clients = [self.primary] + self.fallbacks
        for client in clients:
            try:
                async for chunk in client.stream_generate(messages, tools, temperature):
                    yield chunk
                return
            except Exception:
                continue
        raise RuntimeError("全部 LLM 流式节点请求失败")
