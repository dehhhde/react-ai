# 实现任务队列：企业级 SSE 连接管理器

> 配套设计文档：[SSE-Connection-Manager-Design.md](file:///d:code/react-ai/wiki/SSE-Connection-Manager-Design.md)
>
> 按 4 个 Phase 分 11 个 Task，验收标准（TR）与设计文档中的 8 个设计目标（G1-G8）一一对应。

---

## Phase 1：MVP — 核心连接管理（约 3 天）

### Task 1: 配置项与异常定义

**对应设计**: §15 配置项一览
**优先级**: high
**状态**: pending
**依赖**: 无
**预估工时**: 2h

**实现步骤**:
1. 在 `src/react_ai/config/settings.py` 新增 `StreamSettings` Pydantic 模型，包含全部 15 项配置（见设计文档 §15）。
2. 将 `streaming` 配置组挂载到全局 `settings` 对象。
3. 新增 `src/react_ai/streaming/exceptions.py`，定义：
   - `ResourceExhaustedError(Exception)` — 连接配额耗尽
   - `ConnectionClosedError(Exception)` — 连接已关闭
   - `ClientGoneError(Exception)` — 客户端主动断开
   - `BackpressureError(Exception)` — 背压触发
4. 在 `streaming/__init__.py` 导出公共异常。

**验收标准**:
- **TR-1.1**: `from react_ai.config.settings import settings; settings.streaming.max_active_connections` 返回 2000。
- **TR-1.2**: 全部 15 项配置均可通过环境变量覆盖（`STREAM_MAX_ACTIVE_CONNECTIONS` 等）。
- **TR-1.3**: `ResourceExhaustedError` 等异常可被正常 import 与 raise。

---

### Task 2: SSEConnection + RingBuffer 核心实现

**对应设计**: §3 核心架构, §4 连接状态机, §7 背压与缓冲区
**优先级**: critical
**状态**: pending
**依赖**: Task 1
**预估工时**: 8h

**实现步骤**:
1. 新建 `src/react_ai/streaming/connection.py`。
2. 实现 `RingBuffer` 类（固定容量环形队列，满时丢弃最旧元素）：
   - `put(item)` 非阻塞，返回是否成功放入（满则丢弃最旧）。
   - `get(timeout)` 阻塞获取，带超时。
   - `size` / `capacity` / `clear()` 属性与方法。
3. 实现 `ConnectionState` / `CloseReason` 枚举（见设计文档 §4.1, §4.3）。
4. 实现 `SSEConnection` 类：
   - 构造函数接收 `conn_id / tenant_id / user_id / session_id / settings`。
   - 状态字段：`state, close_reason, created_at, last_activity, last_heartbeat_sent, last_heartbeat_ack`。
   - 通道字段：`event_queue: RingBuffer, cancel_event: asyncio.Event, _agent_task, _writer, _heartbeat_task`。
   - 统计字段：`events_sent, events_dropped, bytes_sent`。
   - 核心方法 `run_stream(agent_task) -> AsyncGenerator[str, None]`：
     - 从 `event_queue` 消费事件 → 写出到 `_writer` → `drain()` → 更新 `last_activity`。
     - 捕获 `BrokenPipeError / ConnectionResetError` → `mark_closed(TRANSPORT_GONE)`。
     - 捕获 `CancelledError` → `mark_closed(CLIENT_GONE)` 并 re-raise。
     - 内部循环带超时（2s）以便及时响应关闭信号。
   - `attach(writer)` 绑定 writer，写 SSE headers，状态 CREATED → ACTIVE。
   - `run_heartbeat()` 心跳循环（按 `heartbeat_interval` 发送 `: heartbeat {seq}\nid: {seq}\n\n`）。
   - `mark_closed(reason)` 状态机转移 + `cancel_event.set()`。
   - `_cleanup()` 资源清理链（取消子 Task → 关闭 writer → 清空队列 → 引用置空）。
   - `to_public_info() / to_detail()` 供诊断端点使用。

**验收标准**:
- **TR-2.1**: RingBuffer 容量 4，连续 put 5 个元素 → get 返回元素 2-5（元素 1 被丢弃）。
- **TR-2.2**: `run_stream` 中 writer.close() 后再 put 事件 → 状态变为 CLOSED，`close_reason == TRANSPORT_GONE`。
- **TR-2.3**: `mark_closed` 后 `cancel_event.is_set() == True`。
- **TR-2.4**: `_cleanup()` 后 `_writer / _heartbeat_task / _agent_task` 均为 None，`event_queue.size == 0`。
- **TR-2.5**: `run_stream` 产出的 SSE 帧格式为 `data: {json}\n\n`，心跳帧为 `: heartbeat {n}\nid: {n}\n\n`。

---

### Task 3: SSEConnectionManager 基础（Registry + 配额闸门）

**对应设计**: §3 核心架构, §8 并发限流与资源配额, §9 多租户隔离
**优先级**: critical
**状态**: pending
**依赖**: Task 2
**预估工时**: 6h

**实现步骤**:
1. 新建 `src/react_ai/streaming/manager.py`。
2. 实现 `SSEConnectionManager` 单例类（线程安全，使用 `asyncio.Lock`）：
   - `_connections: dict[str, SSEConnection]` 注册中心。
   - `_tenant_counts: dict[str, int]` 租户级计数器。
   - `acquire_slot(tenant_id)` 双重闸门（全局 + 租户），超限抛 `ResourceExhaustedError`。
   - `release_slot(tenant_id)` 释放配额（对应计数 -1，归零即移除键）。
   - `create_connection(...)` 创建 Connection 并注册到 Registry。
   - `remove_connection(conn_id, tenant_id=None)` 移除连接，tenant_id 非空时强制匹配（防越权删除）。
   - `get(conn_id) / iter_active() / active_count / tenant_counts_snapshot / get_stats()` 查询接口。
3. 提供 `get_connection_manager()` 工厂函数，返回进程级单例。
4. 在 `streaming/__init__.py` 导出。

**验收标准**:
- **TR-3.1**: 并发调用 `acquire_slot` 达到 `max_active_connections=5` 上限后第 6 次抛 `ResourceExhaustedError`。
- **TR-3.2**: 同一 tenant_id 达到 `max_per_tenant=2` 上限后第 3 次抛 `ResourceExhaustedError`，但另一个 tenant_id 仍可成功获取。
- **TR-3.3**: `remove_connection(conn_id, tenant_id="wrong")` 抛 `PermissionError`（防越权删除）。
- **TR-3.4**: `get_stats()` 返回包含 `total_active / tenant_counts / state_distribution` 的字典。
- **TR-3.5**: `get_connection_manager()` 两次调用返回同一对象。

---

### Task 4: RunContext 外部取消事件接入

**对应设计**: §5.4 CancellationToken 穿透链
**优先级**: high
**状态**: pending
**依赖**: 无
**预估工时**: 2h

**实现步骤**:
1. 查看 `src/react_ai/core/context.py` 现有 `cancel()` / `check_cancelled()` 实现。
2. 扩展 `RunContext`：
   - 新增 `_external_cancel_event: asyncio.Event | None = None` 字段。
   - 新增 `bind_cancel_event(event: asyncio.Event)` 方法，绑定外部取消信号。
   - 修改 `check_cancelled()`：原有本地标记检查 + 外部事件检查，任一命中即抛 `CancelledError`。
3. 验证现有 `test_engine.py` / `test_orchestration.py` 不受影响。

**验收标准**:
- **TR-4.1**: `ctx.bind_cancel_event(event); event.set(); ctx.check_cancelled()` 抛 `CancelledError`。
- **TR-4.2**: 未绑定外部事件时，`check_cancelled()` 行为与改动前一致（仅本地标记生效）。
- **TR-4.3**: 现有全部单测通过（回归）。

---

### Task 5: 路由层最小接入

**对应设计**: §13.2 路由层调用方式, §14 集成方案
**优先级**: high
**状态**: pending
**依赖**: Task 3, Task 4
**预估工时**: 3h

**实现步骤**:
1. 修改 `api/routes/agent.py` 的 `chat_stream_endpoint`：
   - 调用 `manager.acquire_slot(tenant_id)` 获取配额（异常转 503/429）。
   - `manager.create_connection(...)` 创建连接。
   - `ctx.bind_cancel_event(connection.cancel_event)` 注入取消信号。
   - `asyncio.create_task(agent.stream(...))` 创建 Agent Task。
   - `connection.run_stream(agent_task)` 包装 generator。
   - `finally` 中 `manager.remove_connection(conn_id)`。
2. 修改 `resume_run_stream` 同样接入。
3. StreamingResponse 增加 headers：`Cache-Control: no-cache, Connection: keep-alive, X-Accel-Buffering: no`。
4. 暂不启动 HeartbeatScheduler（Phase 2 接入），Manager.start() 留空。

**验收标准**:
- **TR-5.1**: POST `/api/v1/agent/chat/stream` 正常返回 `text/event-stream`，事件格式正确。
- **TR-5.2**: 客户端中途断开连接 → `manager.active_count` 在 1s 内归零，无 orphan task。
- **TR-5.3**: 并发 6 个连接（配置 max=5）→ 第 6 个返回 503。
- **TR-5.4**: `/chat`（非流式）行为不受影响。

---

### Task 6: 单元测试（核心连接管理）

**对应设计**: §16.1 单元测试
**优先级**: high
**状态**: pending
**依赖**: Task 2, Task 3
**预估工时**: 4h

**实现步骤**:
1. 新建 `tests/unit/test_sse_connection.py`。
2. 用例清单：
   - `test_ring_buffer_drops_oldest_when_full`：TR-2.1。
   - `test_transport_disconnect_mark_closed`：mock writer.write 抛 BrokenPipeError → CLOSED/TRANSPORT_GONE。
   - `test_cancel_signal_propagates_to_agent`：connection.mark_closed → cancel_event set → RunContext.check_cancelled 抛 CancelledError。
   - `test_cleanup_releases_all_resources`：TR-2.4，断言内部引用均为 None。
   - `test_manager_global_quota_exhaustion`：TR-3.1。
   - `test_manager_tenant_quota_isolation`：TR-3.2。
   - `test_manager_cross_tenant_removal_rejected`：TR-3.3。
   - `test_heartbeat_frame_format`：TR-2.5，断言心跳帧以 `: heartbeat` 开头。
   - `test_sse_event_frame_format`：断言业务事件帧为 `data: {...}\n\n`。
   - `test_connection_state_transitions`：CREATED → ACTIVE → CLOSING → CLOSED 全路径。

**验收标准**:
- **TR-6.1**: 全部 10 项单测通过。
- **TR-6.2**: 测试不依赖真实网络/数据库，使用 asyncio mock。

---

## Phase 2：心跳 + 背压（约 2 天）

### Task 7: HeartbeatScheduler + ZombieReaper

**对应设计**: §6 心跳保活策略, §10.2 定时僵尸清扫
**优先级**: high
**状态**: pending
**依赖**: Task 3
**预估工时**: 5h

**实现步骤**:
1. 新建 `src/react_ai/streaming/heartbeat.py`。
2. 实现 `HeartbeatScheduler`：
   - 后台 asyncio.Task，每 `heartbeat_interval` 秒 tick。
   - 遍历 Manager 中 ACTIVE 连接，调用 `conn.send_heartbeat(seq)`。
   - 发送失败 → `conn.mark_closed(TRANSPORT_GONE)`。
   - 检查心跳超时：`now - last_heartbeat_ack > probe_count * interval` → `mark_closed(HEARTBEAT_TIMEOUT)`。
   - `start() / stop()` 生命周期方法。
3. 实现 `ZombieReaper`：
   - 后台 asyncio.Task，每 `zombie_reaper_interval`（默认 60s）tick。
   - 扫描全部连接，清理：心跳已不确定且超时的、生命周期超过 `connection_timeout` 的。
4. 集成到 `SSEConnectionManager.start() / shutdown()`。

**验收标准**:
- **TR-7.1**: HeartbeatScheduler 启动后每 15s 向全部 ACTIVE 连接发送心跳帧。
- **TR-7.2**: mock 某连接 3 个周期无心跳 ACK → 状态变为 CLOSED，`close_reason == HEARTBEAT_TIMEOUT`。
- **TR-7.3**: 连接存在时间 > `connection_timeout=300s` 且状态非 CLOSED → 被 ZombieReaper 清理。
- **TR-7.4**: `manager.shutdown()` 后 HeartbeatScheduler 和 ZombieReaper 的 Task 全部被 cancel。

---

### Task 8: 背压三级熔断

**对应设计**: §7 背压与缓冲区管理
**优先级**: high
**状态**: pending
**依赖**: Task 2
**预估工时**: 4h

**实现步骤**:
1. 在 `SSEConnection` 中实现背压逻辑：
   - L1 温和：`event_queue.size / capacity > backpressure_warn_ratio(0.6)` → 记录 WARN 日志 + Metrics。
   - L2 强硬：`> backpressure_critical_ratio(0.9)` → 启用关键事件过滤（仅保留 RUN_START / RUN_END / ERROR / TOOL_APPROVAL_REQUIRED，其余合并为 digest）。
   - L3 熔断：`events_dropped > slow_consumer_max_drops(100)` → `mark_closed(BACKPRESSURE)`。
2. 在 `event_queue.put` 满时 `events_dropped += 1`。
3. 添加 `buffer_usage_ratio` 计算属性供指标采集。
4. 关键事件过滤函数 `_is_critical_event(event_type)`。

**验收标准**:
- **TR-8.1**: 连续 put 100 个事件超过 RingBuffer 容量 → `events_dropped >= 1`。
- **TR-8.2**: buffer 使用率 > 0.9 时，非关键事件（如 OBSERVATION）被过滤，关键事件（RUN_END）仍保留。
- **TR-8.3**: `events_dropped > 100` 后连接状态变为 CLOSED，`close_reason == BACKPRESSURE`。
- **TR-8.4**: `buffer_usage_ratio` 返回 0.0 ~ 1.0 之间的浮点数。

---

## Phase 3：可观测 + 优雅停机（约 2 天）

### Task 9: MetricsCollector SSE 维度指标

**对应设计**: §11 可观测性集成
**优先级**: medium
**状态**: pending
**依赖**: Task 3, Task 7, Task 8
**预估工时**: 4h

**实现步骤**:
1. 在 `src/react_ai/observability/metrics.py` 新增 SSE 连接维度指标：
   - `react_ai_sse_connections_total{state}` counter
   - `react_ai_sse_connections_closed_total{reason, tenant_id}` counter
   - `react_ai_sse_active_connections` gauge
   - `react_ai_sse_active_connections_by_tenant{tenant_id}` gauge
   - `react_ai_sse_events_sent_total{event_type, tenant_id}` counter
   - `react_ai_sse_events_dropped_total{reason, tenant_id}` counter
   - `react_ai_sse_heartbeat_sent_total{tenant_id}` counter
   - `react_ai_sse_heartbeat_missed_total{tenant_id}` counter
   - `react_ai_sse_connection_duration_seconds` histogram
   - `react_ai_sse_buffer_usage_ratio{tenant_id}` gauge
2. 在 `SSEConnection.mark_closed()` / `run_stream()` / `run_heartbeat()` 中埋点。
3. 在 `to_prometheus_text()` 中导出。
4. 在 `/metrics/snapshot` 中也包含。

**验收标准**:
- **TR-9.1**: `/metrics` 端点输出包含全部 10 项 SSE 指标。
- **TR-9.2**: 关闭一个连接后，`react_ai_sse_connections_closed_total{reason="..."}` 增加 1。
- **TR-9.3**: `react_ai_sse_active_connections` gauge 与 `manager.active_count` 一致。

---

### Task 10: FastAPI lifespan 集成 + 诊断端点

**对应设计**: §12.2 优雅停机, §13.1 FastAPI lifespan, §11.2 实时诊断端点
**优先级**: high
**状态**: pending
**依赖**: Task 7, Task 9
**预估工时**: 4h

**实现步骤**:
1. 修改 `api/main.py`：
   - 将 `FastAPI(...)` 改为 `FastAPI(lifespan=lifespan)`。
   - 实现 `lifespan` async context manager：
     - startup: `manager = get_connection_manager(); await manager.start()`
     - shutdown: `await manager.shutdown(timeout=settings.streaming.shutdown_grace_period)`
2. 在 `api/routes/observability.py` 新增端点：
   - `GET /connections` — 活跃连接列表（支持 `?tenant_id=` 筛选）。
   - `GET /connections/{conn_id}` — 单连接详细状态。
3. `SSEConnectionManager.shutdown(timeout)`：
   - 遍历全部 ACTIVE 连接，发送 shutdown SSE 事件。
   - `asyncio.wait` 等待所有连接在 timeout 内自行关闭。
   - 超时仍未关闭的连接强制 `mark_closed(MANAGER_SHUTDOWN)`。
   - 停止 HeartbeatScheduler / ZombieReaper。

**验收标准**:
- **TR-10.1**: 启动服务后 `GET /connections` 返回 `{"total_active": 0, "connections": []}`。
- **TR-10.2**: 建立一个 SSE 连接后 `GET /connections` 返回该连接信息，`total_active == 1`。
- **TR-10.3**: 服务收到 SIGTERM（uvicorn reload 模拟）时，5s 内全部连接关闭，无 orphan task。
- **TR-10.4**: `/metrics` 仍正常导出，不影响现有指标。

---

## Phase 4：降级策略 + 容错（约 1 天）

### Task 11: 熔断器 + 降级策略

**对应设计**: §12 降级与熔断
**优先级**: medium
**状态**: pending
**依赖**: Task 3, Task 7
**预估工时**: 3h

**实现步骤**:
1. 集成项目现有熔断器（`src/react_ai/tools/resilience.py` 中的 CircuitBreaker 或复用其逻辑）到 `SSEConnectionManager`：
   - `acquire_slot` 失败累计，达到阈值打开。
   - 打开期间直接抛 `ServiceUnavailableError`。
   - 恢复时间后 half-open，允许少量探测。
2. 资源饱和降级策略（按 `degrade_at_ratio=0.8`）：
   - 活跃连接 > 80% 上限 → 关闭心跳 pong 要求（仅发不要求 ACK）。
   - > 85% → 新连接临时队列容量减半。
   - > 90% → 按租户优先级选择性拒绝（默认拒绝低优先级，可配置）。
   - > 95% → 关闭 DEBUG 级别事件日志。
3. Agent 执行超时：`run_stream` 外包 `asyncio.wait_for(..., timeout=connection_timeout)`，超时 `mark_closed(TIMEOUT_IDLE)`。

**验收标准**:
- **TR-11.1**: 连续 5 次 `acquire_slot` 内部异常 → 熔断器打开，后续请求直接 503。
- **TR-11.2**: 活跃连接达到 80% 上限后新建连接的 `event_queue.capacity` 为原配置的 1/2。
- **TR-11.3**: Agent 执行超过 `connection_timeout=300s` → 连接自动关闭，`close_reason == TIMEOUT_IDLE`。
- **TR-11.4**: 熔断器关闭 30s 后恢复 half-open，探测成功恢复 closed。

---

### Task 12: 集成测试 + 压测验证

**对应设计**: §16.2 集成测试, §16.3 压测
**优先级**: high
**状态**: pending
**依赖**: Task 1-11
**预估工时**: 4h

**实现步骤**:
1. 新建 `tests/integration/test_sse_streaming.py`：
   - `test_lifespan_start_shutdown`：TR-10.3。
   - `test_http_transport_disconnect`：httpx.AsyncClient 发起 SSE → 中途 close() → 验证连接清理。
   - `test_concurrent_quota_limit`：并发创建 max+1 连接 → 第 N+1 个抛 ResourceExhaustedError。
   - `test_tenant_quota_isolation`：TR-3.2（集成层面）。
   - `test_graceful_shutdown_with_active_connections`：建立 3 个连接后触发 shutdown → 全部 CLOSED。
   - `test_backpressure_slow_consumer`：mock 慢客户端 → 验证背压 L3 熔断。
2. 运行全量单测回归，确保无破坏。
3. 可选：用 `wrk` 或 `k6` 脚本做 2000 并发 SSE 压测（本地受限于 PG/Redis 时使用假数据）。

**验收标准**:
- **TR-12.1**: 集成测试全部通过。
- **TR-12.2**: 全量单测（排除 PG/Redis 依赖）通过：`pytest tests/unit --ignore=test_aftersales_tools.py --ignore=test_memory.py`。
- **TR-12.3**: 无 orphan asyncio Task 警告（Python 3.12+ TaskGroup strict 或 tracemalloc 验证）。

---

## 全局验收标准（对应设计目标 G1-G8）

| 设计目标 | 验收 Task | 验证方式 |
|---|---|---|
| **G1 零泄漏** | Task 2, 6, 12 | tracemalloc 验证 Connection 实例数不增长；无 orphan task warning |
| **G2 自愈断连** | Task 7 | 心跳超时场景下 45s 内判定并关闭，静默断连率 < 1% |
| **G3 资源可控** | Task 3, 12 | 并发超限时返回 503/429，实际连接数 ≤ 配置上限 |
| **G4 租户隔离** | Task 3, 9 | 配额/指标/日志全部带 tenant_id，跨租户删除被拒 |
| **G5 背压保护** | Task 8 | RingBuffer 有界，慢客户端被 L3 熔断断开 |
| **G6 优雅停机** | Task 10 | SIGTERM 后 5s 内全部连接 CLOSED，shutdown 事件已发送 |
| **G7 生产可观测** | Task 9, 10 | /metrics 含 10 项 SSE 指标，/connections 端点可用 |
| **G8 透明集成** | Task 5 | Agent 业务代码零改动，路由层 15 行内完成接入 |

---

## 依赖关系图

```
Task 1 (配置)
   │
   ▼
Task 2 (Connection+RingBuffer) ── Task 4 (RunContext 取消)
   │                                    │
   ▼                                    ▼
Task 3 (Manager) ────────────► Task 5 (路由接入)
   │                              │
   ├── Task 6 (单测)              │
   │                              │
   ├── Task 7 (心跳+清扫) ◄───────┘
   │
   ├── Task 8 (背压)
   │
   ├── Task 9 (指标) ── Task 10 (lifespan+诊断)
   │                        │
   └── Task 11 (熔断降级) ◄─┘
            │
            ▼
       Task 12 (集成测试)
```

---

*文档版本：v1.0 | 2026-11-06 | 配套设计文档：SSE-Connection-Manager-Design.md*