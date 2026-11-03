"""企业级工具注册表与权限控制."""
import uuid
from typing import Any, Dict, List, Optional
from react_ai.core.context import RunContext
from react_ai.schemas.tools import ToolDefinition
from react_ai.tools.base import BaseTool, ToolContext, ToolExecutionError, ToolResult
from react_ai.tools.sandbox import ToolSandbox


class ToolRegistry:
    def __init__(self, sandbox: Optional[ToolSandbox] = None):
        self._tools: Dict[str, BaseTool] = {}
        self.sandbox = sandbox or ToolSandbox()

    def register(self, tool: BaseTool) -> None:
        """注册工具。重名注册直接报错，避免静默覆盖引发的线上事故."""
        if tool.name in self._tools:
            raise ValueError(f"工具 [{tool.name}] 已注册，禁止重复注册（如需替换请先 unregister）")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否成功."""
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_definitions(
        self,
        *,
        tags: Optional[List[str]] = None,
        side_effects: Optional[List[str]] = None,
    ) -> List[ToolDefinition]:
        """列出工具定义，支持按标签/副作用过滤（per-agent 工具集与租户级启停的基础）."""
        result = []
        for tool in self._tools.values():
            if tags is not None:
                tool_tags = getattr(tool, "tags", []) or []
                if not any(t in tool_tags for t in tags):
                    continue
            if side_effects is not None and tool.side_effects not in side_effects:
                continue
            result.append(tool.get_definition())
        return result

    async def call(self, name: str, arguments: Dict[str, Any], context: Optional[RunContext] = None, tool_call_id: Optional[str] = None, attempt: int = 1) -> ToolResult:
        """规范入口：返回结构化 ToolResult（含 error_type/retryable/replayed），统一走沙箱保证鉴权/HITL/熔断/限流生效.

        失败不抛异常而是返回 ok=False 的 ToolResult，供引擎作为错误 Observation 回喂模型自我纠错。
        """
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"未找到已注册的工具: {name}")

        try:
            return await self.sandbox.run_safe(tool, arguments, context, tool_call_id=tool_call_id, attempt=attempt)
        except ToolExecutionError as terr:
            return terr.result

    async def execute(self, name: str, arguments: Dict[str, Any], context: Optional[RunContext] = None, tool_call_id: Optional[str] = None, attempt: int = 1) -> Any:
        """便捷入口（派生自 call）：成功返回 data，失败抛 ToolExecutionError.

        引擎等需要错误回环的调用方应使用 call()；本入口仅供脚本/测试等
        "失败即异常" 场景使用，两者语义不再独立演化。
        """
        result = await self.call(name, arguments, context, tool_call_id=tool_call_id, attempt=attempt)
        if not result.ok:
            raise ToolExecutionError(result)
        return result.data
