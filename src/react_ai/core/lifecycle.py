"""Agent 生命周期钩子接口."""
from typing import Any, Dict
from react_ai.core.context import RunContext
from react_ai.schemas.events import AgentEvent


class AgentLifecycleHook:
    async def on_run_start(self, context: RunContext, input_query: str) -> None:
        pass

    async def on_step_start(self, context: RunContext, step: int) -> None:
        pass

    async def on_tool_call(self, context: RunContext, tool_name: str, args: Dict[str, Any]) -> None:
        pass

    async def on_tool_result(self, context: RunContext, tool_name: str, result: Any) -> None:
        pass

    async def on_event(self, context: RunContext, event: AgentEvent) -> None:
        pass

    async def on_run_end(self, context: RunContext, output: str) -> None:
        pass

    async def on_error(self, context: RunContext, error: Exception) -> None:
        pass
