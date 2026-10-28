"""Agent 抽象基类定义."""
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from react_ai.agents.base import (
    AgentContext,
    AgentResult,
    AgentStatus,
    AgentStep,
)
from react_ai.core.context import RunContext
from react_ai.schemas.agent import AgentResponse
from react_ai.schemas.events import AgentEvent


class BaseAgent(ABC):
    @abstractmethod
    async def run(self, query: str, context: RunContext) -> AgentResponse:
        """执行同步完整轮次并返回最终响应结果."""

    @abstractmethod
    async def stream(self, query: str, context: RunContext) -> AsyncGenerator[AgentEvent, None]:
        """流式产出思考、工具调用与结果事件."""


__all__ = [
    "AgentContext",
    "AgentResult",
    "AgentStatus",
    "AgentStep",
    "BaseAgent",
]
