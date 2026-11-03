"""工具调用鉴权服务（RBAC/ABAC 钩子）.

在工具执行前判定"该用户/租户是否可用该工具、参数是否越权"。
- 默认实现基于租户 + 工具名白名单（可配置）。
- 生产环境应替换为对接 IAM/权限中心的真实实现（实现 ToolAuthorizationService 协议即可）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Protocol

from react_ai.tools.base import BaseTool, ToolContext


class ToolAuthorizationService(Protocol):
    """鉴权服务协议：在工具执行前调用，未通过则抛 PermissionError."""

    def authorize(
        self,
        tool: BaseTool,
        arguments: Dict[str, Any],
        ctx: ToolContext,
    ) -> None: ...


class DefaultAuthorizationService:
    """默认鉴权实现：基于租户→工具白名单的简单 RBAC。

    规则：
    - 若租户有显式工具白名单，则仅允许白名单内工具；
    - 无白名单的租户默认放行（便于开发期，生产应通过配置收紧）。
    """

    def __init__(self, tenant_tool_allowlist: Dict[str, List[str]] | None = None):
        # tenant_id -> 允许的工具名列表；None 表示不限制
        self.tenant_tool_allowlist: Dict[str, List[str] | None] = tenant_tool_allowlist or {}

    def authorize(
        self,
        tool: BaseTool,
        arguments: Dict[str, Any],
        ctx: ToolContext,
    ) -> None:
        tenant_id = ctx.tenant_id
        allowlist = self.tenant_tool_allowlist.get(tenant_id)
        if allowlist is not None and tool.name not in allowlist:
            raise PermissionError(
                f"租户 [{tenant_id}] 无权调用工具 [{tool.name}]，"
                f"允许的工具: {', '.join(allowlist)}"
            )
        # 扩展点：在此可增加 ABAC 属性级校验，例如：
        # - 写操作工具对单租户的日调用配额
        # - 特定工具的参数越权校验（如 query_user_history 的 user_id 必须等于 ctx.user_id）
