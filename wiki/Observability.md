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

session_logger = logger.bind(thread_id="default:USR_001:SESS_TEST_002")  # 关联键固定，后续调用不必重复传
session_logger.info("node_completed", node="query_order", latency_ms=41.2)
```

每条日志输出单行 JSON：`{"timestamp", "time", "level", "message", "logger", "context", "extra"}`（`time` 为 UTC ISO-8601 毫秒），便于 ELK / Loki 采集与字段检索。要点：

- **级别真实生效**：底层 handler 级别取 `settings.observability.log_level`（`OBSERVABILITY_LOG_LEVEL`），DEBUG/WARNING/ERROR 不再被压平为 INFO。
- **走 stderr**：与脚本 stdout 的机器可读结果分离，`live_e2e_verify.py`/评测器这类输出 JSON 的入口不受污染。
- **`bind(**fields)`**：返回携带固定关联字段的子 logger，共享同一底层 handler；会话级追踪键（`thread_id`）绑一次即可。
- **序列化兜底**：`json.dumps(..., ensure_ascii=False, default=str)`，非可序列化对象（异常、dataclass）不会打崩日志器。
- 关联键约定：跨轮排查统一用 `thread_id`（`{tenant}:{user}:{session}`），辅以 `session_id`/`user_id`——一次会话的全部节点轨迹可用该键在日志系统还原。

## MetricsCollector 指标度量

文件：[observability/metrics.py](file:///d:/code/react-ai/src/react_ai/observability/metrics.py)

```python
class MetricsCollector:
    total_requests: int
    total_tokens: int
    tool_call_counts: dict[str, int]
    tool_error_counts: dict[str, int]

    # 兼容旧接口（ReAct 主链路与生命周期钩子沿用）
    def record_request(self, tokens: int = 0): ...
    def record_tool_call(self, tool_name: str, success: bool): ...

    # 带标签计数器
    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0): ...
    def counter(self, name: str, labels: dict[str, str] | None = None) -> float: ...

    # 直方图：count/sum 全量累计，样本有界（HISTOGRAM_SAMPLE_LIMIT=1024）用于 p50/p95/p99 估算
    def observe(self, name: str, value: float, labels: dict[str, str] | None = None): ...
    def histogram(self, name: str, labels: dict[str, str] | None = None) -> dict: ...      # {count,sum,avg,quantiles}
    def histogram_series(self, name: str) -> list[tuple[dict, dict]]: ...                   # 按标签展开

    def snapshot(self) -> dict: ...
    def to_prometheus_text(self) -> str: ...
    def reset(self): ...          # 测试隔离用；进程内单例，勿在生产调用

metrics = MetricsCollector()
```

线程安全：全部读写经 `threading.Lock`；标签值按 Prometheus 规范转义并加引号，故同一序列在进程内标识与导出文本一致。

`snapshot()` 在旧字段之外新增 `counters`（`{指标名: [{labels, value}]}`）与 `histograms`（`{指标名: [{labels, count, sum, avg, quantiles}]}`）；`to_prometheus_text()` 输出可直接被抓取的文本（计数器 `# TYPE ... counter`，直方图按 `summary` 导出分位数与 `_sum`/`_count`）。项目端点在 [api/main.py](file:///d:/code/react-ai/api/main.py)：

```python
@app.get("/metrics")                       # Prometheus 文本，text/plain; version=0.0.4
async def metrics_endpoint():
    return PlainTextResponse(metrics.to_prometheus_text(), media_type="text/plain; version=0.0.4")

@app.get("/metrics/snapshot")              # 同一份指标的 JSON，便于调试与临时取数
async def metrics_snapshot_endpoint():
    return metrics.snapshot()
```

## 售后图的落地范式（节点埋点 + 会话串行化）

LangGraph 示例 [examples/aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py) 是本模块的参考用法，可复制到其他子图：

| 组件 | 作用 |
|---|---|
| `instrument_node(name, fn)` | 包装节点，产出 `node_latency_seconds{node}`、`decision_total{decision}` 与 `node_completed`/`node_failed` 日志，异步/同步节点皆支持，不改动节点输入输出 |
| `trace_of(state)` / `log_node(state, event, **fields)` | 每条日志固定携带 `thread_id`/`session_id`/`user_id`；`thread_id_of` 缺省按 `{tenant}:{user}:{session}` 推导 |
| `serialize_session(thread_id)` | 进程内 `asyncio.Lock` + `pg_advisory_lock(hashtext(thread_id))` 双层串行化；锁不可用时打 `session_lock_degraded` 并降级继续服务，不拒绝用户 |
| `ainvoke_serialized(app, inputs, config)` | 标准轮次入口：串行执行一轮 + 用 leaf-checkpoint SQL 检查 head 数，分叉则 `checkpoint_fork_total` +1 并打 ERROR 级 `checkpoint_fork_detected` |
| `format_metrics_report()` | 演示脚本结尾打印六类指标汇总，与 `/metrics` 同源数据 |

开关：`ENABLE_SESSION_LOCK`（默认 `true`）与 `SESSION_LOCK_TIMEOUT`（默认 30 秒），见 [Configuration](Configuration.md)。验收一次并行流量：`py scripts/live_e2e_verify.py fork-check --thread <thread_id>`，分叉时退出码 1。

指标口径与业务事件名清单见 [Intent-Recognition-Optimization.md](Intent-Recognition-Optimization.md) §2.3；**逐项速查表（数据落地位置、指标与日志事件清单、采集时机、口径限制、常用查询）见 [Observability Data Catalog](Observability-Data-Catalog.md)**。守护测试 [tests/unit/test_observability.py](file:///d:/code/react-ai/tests/unit/test_observability.py)（采集器与日志器本体）、[tests/unit/test_aftersales_observability.py](file:///d:/code/react-ai/tests/unit/test_aftersales_observability.py)（离线断言六类指标各自增长、日志必带 `thread_id`、同线程并发度恒为 1 而不同线程可并行）。

安全约束：护栏命中只记录规则标识与动作，PII 原文与未脱敏正文一律不落日志；`final_reply` 在日志中截断 60 字。

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
