"""统一大模型接口规范与响应实体."""
from abc import ABC, abstractmethod
from typing import AsyncGenerator, List, Optional
from pydantic import BaseModel, Field
from react_ai.schemas.messages import ChatMessage, MessageRole, ToolCall, UsageStats
from react_ai.schemas.tools import ToolDefinition


class LLMResponse(BaseModel):
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    usage: UsageStats = Field(default_factory=UsageStats)
    raw_response: Optional[dict] = None


class LLMStreamChunk(BaseModel):
    """流式推理事件：delta 为增量文本；流结束时在最后一个 chunk 上给出完整 tool_calls 与 usage."""

    delta: str = ""
    tool_calls: Optional[List[ToolCall]] = None
    usage: Optional[UsageStats] = None


class BaseLLMClient(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        """发起单次模型推理请求.

        tool_choice 传入工具名时强制模型调用该工具 (function calling 结构化输出),
        仅在 tools 非空时生效; None 表示由模型自行决定是否调用.
        """
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

    async def stream_generate_full(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        """流式推理（含工具调用增量拼接）.

        默认实现降级为单次 generate 后一次性给出结果；
        支持真实流式协议的适配器应覆写本方法，逐段产出文本增量，
        并在流结束时给出按 index 拼接完成的 tool_calls。
        """
        res = await self.generate(messages, tools, temperature, max_tokens, tool_choice)
        yield LLMStreamChunk(delta=res.content or "", tool_calls=res.tool_calls, usage=res.usage)

    def _simulate_react_step(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
    ) -> LLMResponse:
        """零 Key 离线演示模式：模拟两步 ReAct（先调用工具，再依据 Observation 作答）."""
        has_tool_observation = any(m.role == MessageRole.TOOL for m in messages)

        if not has_tool_observation and tools:
            tool_to_use = tools[0]
            last_user_msg = next((m.content for m in reversed(messages) if m.role == MessageRole.USER), "")
            expression = "(35 * 12) + 180" if "(35 * 12)" in last_user_msg else "10 + 20"
            return LLMResponse(
                content=f"为了回答问题，我需要使用工具 [{tool_to_use.name}] 计算表达式。",
                tool_calls=[
                    ToolCall(
                        id="call_mock_101",
                        name=tool_to_use.name,
                        arguments={"expression": expression} if tool_to_use.name == "calculate" else {"query": last_user_msg},
                    )
                ],
                usage=UsageStats(prompt_tokens=85, completion_tokens=25, total_tokens=110),
            )

        latest_tool_msg = next((m.content for m in reversed(messages) if m.role == MessageRole.TOOL), "完成")
        return LLMResponse(
            content=f"根据工具执行观察结果（输出：{latest_tool_msg}），最终计算结论为 {latest_tool_msg}。",
            tool_calls=None,
            usage=UsageStats(prompt_tokens=120, completion_tokens=35, total_tokens=155),
        )
