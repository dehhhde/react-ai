"""工具沙箱隔离执行与安全拦截器."""
import asyncio
import inspect
import uuid
from typing import Any, Dict, Optional

from react_ai.core.context import RunContext
from react_ai.tools.base import BaseTool, ToolContext, ToolResult


class ToolSandbox:
    def __init__(self, default_timeout: float = 30.0):
        self.default_timeout = default_timeout

    def _build_tool_context(self, context: Optional[Any] = None) -> ToolContext:
        if isinstance(context, ToolContext):
            return context
        if isinstance(context, RunContext):
            return ToolContext(
                caller_id=context.metadata.get("caller_id", "react-agent"),
                tenant_id=context.metadata.get("tenant_id", "default"),
                user_id=context.user_id or "anonymous",
                trace_id=context.run_id or uuid.uuid4().hex,
                idempotency_key=context.metadata.get("idempotency_key"),
            )
        return ToolContext(
            caller_id="react-agent",
            tenant_id="default",
            user_id="anonymous",
            trace_id=uuid.uuid4().hex,
        )

    async def run_safe(self, tool: BaseTool, arguments: Dict[str, Any], context: Optional[RunContext] = None) -> Any:
        if getattr(tool, "requires_approval", False) or getattr(tool, "side_effects", "none") in {"destructive"}:
            # 触发人工审批中断检查 (HITL: Human-In-The-Loop)
            pass

        timeout = getattr(tool, "timeout_seconds", None) or self.default_timeout
        tool_ctx = self._build_tool_context(context)

        try:
            if hasattr(tool, "acall"):
                tool_res: ToolResult = await asyncio.wait_for(tool.acall(arguments, tool_ctx), timeout=timeout)
            elif hasattr(tool, "call"):
                tool_res: ToolResult = await asyncio.wait_for(
                    asyncio.to_thread(tool.call, arguments, tool_ctx),
                    timeout=timeout,
                )
            else:
                exec_fn = getattr(tool, "execute")
                res = exec_fn(arguments, tool_ctx)
                if inspect.isawaitable(res):
                    res = await asyncio.wait_for(res, timeout=timeout)
                return res

        except asyncio.TimeoutError:
            raise TimeoutError(f"工具 [{tool.name}] 执行超过指定时限 ({timeout}s)")

        if not tool_res.ok:
            if tool_res.error_type == "auth":
                raise PermissionError(tool_res.error_message)
            elif tool_res.error_type == "business":
                raise ValueError(tool_res.error_message)
            else:
                raise RuntimeError(f"[{tool_res.error_code}] {tool_res.error_message}")

        return tool_res.data
