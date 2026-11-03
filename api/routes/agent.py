"""FastAPI Agent 路由 (支持 SSE 流式协议与同步调用)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from react_ai.approval.service import get_approval_service
from react_ai.core.checkpoint import get_checkpoint_store
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.llm.client import create_llm_client
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
    工具本身不持有租户态（tenant/user 经 ToolContext 注入），可安全并发复用。"""
    global _agent
    if _agent is None:
        _agent = ReActAgent(
            llm=create_llm_client(),
            tool_registry=get_registry(),
            approval_service=get_approval_service(),
            checkpoint_store=get_checkpoint_store(),
        )
    return _agent


class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    stream: bool = True


@router.post("/chat")
async def chat_endpoint(req: ChatRequest, agent: ReActAgent = Depends(get_agent)):
    ctx = RunContext(session_id=req.session_id or "default-session")
    response = await agent.run(req.query, ctx)
    return response


@router.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, agent: ReActAgent = Depends(get_agent)):
    ctx = RunContext(session_id=req.session_id or "default-session")

    async def event_generator():
        async for event in agent.stream(req.query, ctx):
            data = event.model_dump_json()
            yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/tools")
async def list_tools(registry: ToolRegistry = Depends(get_registry)):
    return registry.list_definitions()


@router.post("/runs/{run_id}/resume")
async def resume_run_stream(run_id: str, agent: ReActAgent = Depends(get_agent)):
    """审批决策后从挂起点恢复运行 (SSE 流式)."""
    if get_checkpoint_store().load(run_id) is None:
        raise HTTPException(status_code=404, detail=f"未找到运行 {run_id} 的挂起检查点")

    async def event_generator():
        async for event in agent.resume(run_id):
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
