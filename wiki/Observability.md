# Observability 可观测性体系

模块：`react_ai.observability`。三个组件均提供模块级单例，直接导入使用：

```python
from react_ai.observability import tracer, logger, metrics
```

## AgentTracer 链路追踪

文件：[observability/tracer.py](file:///d:/code/react-ai/src/react_ai/observability/tracer.py)

```python
class AgentTracer:
    def __init__(self, service_name: str = "react-ai"): ...

    @asynccontextmanager
    async def start_span(self, name: str, attributes: dict | None = None):
        ...  # 生产对接 opentelemetry.trace

tracer = AgentTracer()
```

用法：

```python
async with tracer.start_span("agent.run", {"session_id": ctx.session_id}) as span:
    ...  # span 为 dict，可写入属性
```

设计语义：为 Agent 思考、工具调用、模型交互记录 Span 树；`RunContext.run_id` 即 TraceID。当前为 OTel 对接骨架，OTLP 端点由 `settings.observability.otlp_endpoint` 配置。

## StructuredLogger 结构化日志

文件：[observability/logger.py](file:///d:/code/react-ai/src/react_ai/observability/logger.py)

```python
logger = StructuredLogger(name="react-ai")

logger.info("agent_run_end", session_id=ctx.session_id, tokens=usage.total_tokens)
logger.warning("tool_retry", tool="http", attempt=2)
logger.error("llm_failover", primary="deepseek", error=str(e))
```

每条日志输出单行 JSON：`{"timestamp", "level", "message", "context", "extra"}`，便于 ELK / Loki 采集与字段检索。关键字段（session_id、trace_id）建议通过 kwargs 显式关联。

## MetricsCollector 指标度量

文件：[observability/metrics.py](file:///d:/code/react-ai/src/react_ai/observability/metrics.py)

```python
class MetricsCollector:
    total_requests: int
    total_tokens: int
    tool_call_counts: dict[str, int]
    tool_error_counts: dict[str, int]

    def record_request(self, tokens: int = 0): ...
    def record_tool_call(self, tool_name: str, success: bool): ...
    def snapshot(self) -> dict: ...

metrics = MetricsCollector()
```

`snapshot()` 输出可直接暴露给 Prometheus exporter 或健康检查端点：

```python
@app.get("/metrics")
async def metrics_endpoint():
    return metrics.snapshot()
```

## 与生命周期钩子集成

推荐通过 `AgentLifecycleHook`（见 [Core Engine](Core-Engine.md)）无侵入采集：

```python
class ObsHook(AgentLifecycleHook):
    async def on_run_end(self, context, output):
        metrics.record_request(tokens=context.used_tokens)
    async def on_tool_result(self, context, tool_name, result):
        metrics.record_tool_call(tool_name, success=True)
    async def on_error(self, context, error):
        logger.error("agent_error", run_id=context.run_id, error=str(error))

ReActAgent(llm=llm, tool_registry=registry, hooks=[ObsHook()])
```
