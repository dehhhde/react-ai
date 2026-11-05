"""FastAPI Agent 路由 (支持 SSE 流式协议与同步调用)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from react_ai.approval.service import get_approval_service
from react_ai.core.checkpoint import get_checkpoint_store
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.llm.client import create_llm_client
from react_ai.memory import SessionKey, create_memory_from_settings
from react_ai.observability import build_audit_store_from_settings, set_audit_store
from react_ai.tools.registry import ToolRegistry
from react_ai.tools.sandbox import ToolSandbox
from react_ai.tools.builtin.retrieval import KnowledgeRetrievalTool
from react_ai.tools.aftersales import (
    QueryOrderTool,
    QueryLogisticsTool,
    SearchPolicyTool,
    QueryUserHistoryTool,
    CreateRefundTool,
    CreateExchangeOrderTool,
    InterceptShipmentTool,
    SendNotificationTool,
    EscalateToHumanTool,
)

router = APIRouter(prefix="/api/v1/agent", tags=["Agent"])


def _build_registry() -> ToolRegistry:
    approval_service = get_approval_service()
    reg = ToolRegistry(sandbox=ToolSandbox(approval_service=approval_service))
    reg.register(KnowledgeRetrievalTool())
    for _t in [
        QueryOrderTool(),
        QueryLogisticsTool(),
        SearchPolicyTool(),
        QueryUserHistoryTool(),
        CreateRefundTool(),
        CreateExchangeOrderTool(),
        InterceptShipmentTool(),
        SendNotificationTool(),
        EscalateToHumanTool(),
    ]:
        reg.register(_t)
    return reg


_registry: Optional[ToolRegistry] = None
_agent: Optional[ReActAgent] = None


def get_registry() -> ToolRegistry:
    """懒构建共享注册表：避免 import 期副作用，测试可用 app.dependency_overrides 替换."""
    global _registry
    if _registry is None:
        # 审计存储按配置构建（AUDIT_BACKEND: memory|postgres|elasticsearch），
        # 依赖服务不可用/未配置时自动降级内存，不阻断服务启动
        set_audit_store(build_audit_store_from_settings())
        _registry = _build_registry()
    return _registry


def get_agent() -> ReActAgent:
    """懒构建共享 Agent：单次 ReAct 运行状态由每次请求新建的 RunContext 承载，
    工具本身不持有租户态（tenant/user 经 ToolContext 注入），可安全并发复用。
    会话记忆按配置装配（差距分析 2.4）：MEMORY_TYPE=in_memory|redis，
    TTL 受 SESSION_TTL 控制；redis 不可用时降级内存并告警，不阻断服务启动。"""
    global _agent
    if _agent is None:
        _agent = ReActAgent(
            llm=create_llm_client(),
            tool_registry=get_registry(),
            approval_service=get_approval_service(),
            checkpoint_store=get_checkpoint_store(),
            memory=create_memory_from_settings(),
        )
    return _agent


class ChatRequest(BaseModel):
    query: str
    # 差距分析 2.2：session_id 必填，禁止 default-session 兜底；缺失时由 Pydantic 返回 422
    session_id: str = Field(..., min_length=1, description="会话 ID（必填，由客户端或网关保证唯一）")
    user_id: Optional[str] = Field(default=None, description="用户 ID；缺失时落入独立的匿名命名空间")
    tenant_id: Optional[str] = Field(default=None, description="租户 ID；缺失时落入独立的匿名命名空间")
    stream: bool = True


def _build_context(req: ChatRequest) -> RunContext:
    """按身份三元组构造 RunContext：session_id 必填，tenant/user 缺失时落入匿名命名空间。"""
    return RunContext(
        session_id=req.session_id,
        user_id=req.user_id,
        tenant_id=req.tenant_id,
    )


@router.post("/chat")
async def chat_endpoint(req: ChatRequest, agent: ReActAgent = Depends(get_agent)):
    ctx = _build_context(req)
    response = await agent.run(req.query, ctx)
    return response


@router.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, agent: ReActAgent = Depends(get_agent)):
    ctx = _build_context(req)

    async def event_generator():
        async for event in agent.stream(req.query, ctx):
            data = event.model_dump_json()
            yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/tools")
async def list_tools(registry: ToolRegistry = Depends(get_registry)):
    return registry.list_definitions()


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    agent: ReActAgent = Depends(get_agent),
):
    """按身份三元组查询会话历史（差距分析 2.2）。

    访问他人会话时因三元组不匹配查不到数据，统一返回 404，
    既不泄露会话是否存在，也保证跨用户/跨租户不可见。
    """
    if agent.memory is None:
        raise HTTPException(status_code=503, detail="会话记忆未启用")
    try:
        key = SessionKey.resolve(tenant_id=tenant_id, user_id=user_id, session_id=session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    history = agent.memory.load_session(key)
    if not history:
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前身份")
    return {
        "session_id": session_id,
        "messages": [m.model_dump(mode="json") for m in history],
    }


@router.post("/runs/{run_id}/resume")
async def resume_run_stream(run_id: str, agent: ReActAgent = Depends(get_agent)):
    """审批决策后从挂起点恢复运行 (SSE 流式)."""
    if get_checkpoint_store().load(run_id) is None:
        raise HTTPException(status_code=404, detail=f"未找到运行 {run_id} 的挂起检查点")

    async def event_generator():
        async for event in agent.resume(run_id):
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
