"""企业级工具注册表与权限控制."""
import uuid
from typing import Any, Dict, List, Optional
from react_ai.core.context import RunContext
from react_ai.schemas.tools import ToolDefinition
from react_ai.tools.base import BaseTool, ToolContext, ToolResult
from react_ai.tools.sandbox import ToolSandbox


class ToolRegistry:
    def __init__(self, sandbox: Optional[ToolSandbox] = None):
        self._tools: Dict[str, BaseTool] = {}
        self.sandbox = sandbox or ToolSandbox()

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_definitions(self) -> List[ToolDefinition]:
        return [tool.get_definition() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: Dict[str, Any], context: Optional[RunContext] = None) -> Any:
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"未找到已注册的工具: {name}")
        return await self.sandbox.run_safe(tool, arguments, context)

    async def call(self, name: str, arguments: Dict[str, Any], context: Optional[RunContext] = None) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"未找到已注册的工具: {name}")

        if isinstance(context, ToolContext):
            tool_ctx = context
        elif isinstance(context, RunContext):
            tool_ctx = ToolContext(
                caller_id=context.metadata.get("caller_id", "react-agent"),
                tenant_id=context.metadata.get("tenant_id", "default"),
                user_id=context.user_id or "anonymous",
                trace_id=context.run_id or uuid.uuid4().hex,
                idempotency_key=context.metadata.get("idempotency_key"),
            )
        else:
            tool_ctx = ToolContext(
                caller_id="react-agent",
                tenant_id="default",
                user_id="anonymous",
                trace_id=uuid.uuid4().hex,
            )

        if hasattr(tool, "acall"):
            return await tool.acall(arguments, tool_ctx)
        return tool.call(arguments, tool_ctx)
