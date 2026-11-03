"""可观测性查询路由：Prometheus 指标、审计回放、span 查询."""
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from react_ai.observability import get_audit_store, metrics, tracer

router = APIRouter(prefix="/api/v1/observability", tags=["Observability"])


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus 抓取端点：工具调用计数/耗时摘要/熔断状态 gauge."""
    return metrics.to_prometheus()


@router.get("/metrics/snapshot")
async def metrics_snapshot():
    """指标结构化快照（含 P50/P90/P99），供管理面或调试使用."""
    return metrics.snapshot()


@router.get("/audit")
async def query_audit(
    trace_id: Optional[str] = Query(default=None, description="按 trace 关联查询"),
    tool_name: Optional[str] = Query(default=None, description="按工具名过滤"),
    tenant_id: Optional[str] = Query(default=None, description="按租户过滤"),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """工具调用审计回放：谁、何时、何参数（已脱敏）、结果、trace 关联."""
    records = get_audit_store().query(
        trace_id=trace_id, tool_name=tool_name, tenant_id=tenant_id, limit=limit
    )
    return [r.model_dump() for r in records]


@router.get("/spans")
async def recent_spans(limit: int = Query(default=50, ge=1, le=1000)):
    """最近 span 记录（内存实现）；配置 OTEL_EXPORTER_OTLP_ENDPOINT 后以 Jaeger/Tempo 视图为准."""
    return tracer.recent_spans(limit)
