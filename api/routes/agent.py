from typing import Optional
"""FastAPI Agent 路由 (支持 SSE 流式协议与同步调用)."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.llm.client import create_llm_client
from react_ai.tools.registry import ToolRegistry
from react_ai.tools.builtin.retrieval import KnowledgeRetrievalTool

router = APIRouter(prefix="/api/v1/agent", tags=["Agent"])

registry = ToolRegistry()
registry.register(KnowledgeRetrievalTool())
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
    registry.register(_t)
agent = ReActAgent(llm=create_llm_client(), tool_registry=registry)


class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    stream: bool = True


@router.post("/chat")
async def chat_endpoint(req: ChatRequest):
    ctx = RunContext(session_id=req.session_id or "default-session")
    response = await agent.run(req.query, ctx)
    return response


@router.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    ctx = RunContext(session_id=req.session_id or "default-session")

    async def event_generator():
        async for event in agent.stream(req.query, ctx):
            data = event.model_dump_json()
            yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/tools")
async def list_tools():
    return registry.list_definitions()
