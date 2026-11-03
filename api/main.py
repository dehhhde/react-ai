"""企业级 FastAPI 后端接入入口."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from api.routes.agent import router as agent_router
from api.routes.approvals import router as approvals_router
from api.routes.observability import router as observability_router
from react_ai.observability import metrics

app = FastAPI(
    title="react-ai Enterprise Agent API",
    version="0.1.0",
    description="企业级 ReAct AI 智能体运行时 REST & SSE 服务端",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_router)
app.include_router(approvals_router)
app.include_router(observability_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "react-ai"}


@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus 抓取端点：带标签计数器与分位数指标统一文本导出（§2.3）。"""
    return PlainTextResponse(metrics.to_prometheus_text(), media_type="text/plain; version=0.0.4")


@app.get("/metrics/snapshot")
async def metrics_snapshot_endpoint():
    """同一份指标的 JSON 快照，便于调试与看板临时取数。"""
    return metrics.snapshot()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
