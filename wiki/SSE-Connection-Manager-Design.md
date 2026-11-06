# 企业级 SSE 连接管理器设计文档

> 对标大厂（Spring Cloud Gateway / Go-Kratos / Node.js Socket.IO）连接管理架构，为 react-ai Agent 服务提供生产级 SSE 长连接治理能力。

---

## 目录

1. [背景与问题分析](#1-背景与问题分析)
2. [设计目标与非目标](#2-设计目标与非目标)
3. [核心架构](#3-核心架构)
4. [连接状态机](#4-连接状态机)
5. [断连检测机制](#5-断连检测机制)
6. [心跳保活策略](#6-心跳保活策略)
7. [背压与缓冲区管理](#7-背压与缓冲区管理)
8. [并发限流与资源配额](#8-并发限流与资源配额)
9. [多租户隔离](#9-多租户隔离)
10. [资源清理与内存防护](#10-资源清理与内存防护)
11. [可观测性集成](#11-可观测性集成)
12. [降级与熔断](#12-降级与熔断)
13. [API 设计](#13-api-设计)
14. [与现有代码集成方案](#14-与现有代码集成方案)
15. [配置项一览](#15-配置项一览)
16. [测试策略](#16-测试策略)
17. [对标大厂方案对比](#17-对标大厂方案对比)
18. [落地路线图](#18-落地路线图)

---

## 1. 背景与问题分析

### 1.1 当前实现现状

`api/routes/agent.py` 的 SSE 端点使用 FastAPI `StreamingResponse` 包裹原生 Python async generator：

```python
@router.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, agent: ReActAgent = Depends(get_agent)):
    ctx = _build_context(req)

    async def event_generator():
        async for event in agent.stream(req.query, ctx):
            data = event.model_dump_json()
            yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

### 1.2 存在的问题

| # | 问题 | 根因 | 影响 | 严重性 |
|---|---|---|---|---|
| 1 | **客户端断连后服务端继续运行** | async generator 无法感知 HTTP transport 关闭 | LLM token/工具调用持续消耗，结果无人接收 | 🔴 Critical |
| 2 | **无心跳保活** | 长时间无事件产生时，中间代理（Nginx/ALB/CDN）idle timeout 主动断连 | 用户侧偶发「静默断开」，重连体验差 | 🔴 Critical |
| 3 | **协程泄漏** | 断连后 `agent.stream()` 的 asyncio Task 未被 cancel | 事件循环中 orphan task 累积，内存持续增长 | 🔴 Critical |
| 4 | **无并发上限** | 每个 SSE 连接独占一个 asyncio Task，无全局/租户级配额控制 | 恶意或误操作耗尽服务器资源 | 🟡 High |
| 5 | **无背压保护** | 慢客户端导致 FastAPI 写缓冲区持续堆积 | 内存膨胀，OOM 风险 | 🟡 High |
| 6 | **无可观测性** | 无连接数、断连原因、生命周期等指标 | 生产问题完全黑盒，无法容量规划 | 🟡 High |
| 7 | **无线连接清理** | 无定时任务扫描超时连接 | 僵尸连接占用文件描述符 | 🟠 Medium |
| 8 | **无优雅关闭** | 服务进程收到 SIGTERM 时 SSE 连接被硬砍 | 客户端看到网络错误，无收口事件 | 🟠 Medium |

### 1.3 为什么不是简单 try/except 就完事

SSE 断连检测在不同层的表现不同：

```
客户端关闭浏览器
    ↓
TCP FIN → 内核 socket 缓冲区
    ↓
uvicorn/httpx Transport 层触发 StreamWriter.close()
    ↓
async generator 在下次 yield 时抛 BrokenPipeError / GeneratorExit
    ↓                              ↑
    但 agent.stream() 中可能正阻塞在 LLM HTTP 请求上，
    yield 永远不会执行 → Task 永远不会结束
```

**核心难点**：如果当前正在执行阻塞性 I/O（LLM 调用、工具 HTTP 请求），断连信号要穿透多层 await 链才能被业务代码感知。

---

## 2. 设计目标与非目标

### 2.1 设计目标

| # | 目标 | 验收标准 |
|---|---|---|
| G1 | **零泄漏** | 客户端断连后 500ms 内关联 Task 全部被 cancel，无 orphan task |
| G2 | **自愈断连** | 生产环境 SSE 静默断连率 < 1%（心跳保活覆盖代理超时） |
| G3 | **资源可控** | 单进程同时活跃 SSE 连接 ≤ 配置上限，超限时按租户优先级拒绝 |
| G4 | **租户隔离** | 连接配额、指标、日志全部按 tenant_id 维度隔离 |
| G5 | **背压保护** | 慢客户端缓冲区有界（默认 64KB），超限自动断开并记录指标 |
| G6 | **优雅停机** | SIGTERM 时 3s 内排空或关闭全部 SSE 连接，发送 shutdown 事件 |
| G7 | **生产可观测** | `/metrics` 暴露连接数/断连原因/生命周期直方图，`/connections` 端点可查看 |
| G8 | **透明集成** | 改造后 Agent 业务代码零感知，路由层 3 行改动完成接入 |

### 2.2 非目标

- **不实现** SSE → WebSocket 升级（保持纯 SSE 协议）
- **不实现** 跨节点连接路由（单节点连接管理器，多节点通过 Redis Pub/Sub 广播）
- **不实现** 客户端 SDK（仅提供服务端治理）
- **不实现** 消息离线存储与重放（由 CheckpointStore + 客户端重连补偿）

---

## 3. 核心架构

### 3.1 整体分层

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FastAPI / Starlette                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    SSEConnectionManager (单例)                       │    │
│  │                                                                     │    │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐  │    │
│  │  │  Connection   │  │  Registry    │  │      全局资源闸门          │  │    │
│  │  │  (每连接状态)  │  │  (id→obj 映射)│  │  ┌─────┐ ┌─────┐ ┌────┐  │  │    │
│  │  │               │  │              │  │  │ 并发 │ │ 租户 │ │ FD │  │  │    │
│  │  │ - conn_id     │  │ - 活跃连接池  │  │  │ 限流 │ │ 配额 │ │ 上限│  │  │    │
│  │  │ - tenant_id   │  │ - 僵尸清理    │  │  └─────┘ └─────┘ └────┘  │  │    │
│  │  │ - buffer      │  │ - 优雅停机    │  └───────────────────────────┘  │    │
│  │  │ - last_heartbeat│ └──────────────┘                                │    │
│  │  │ - task_handle  │                                                    │    │
│  │  │ - created_at   │  ┌─────────────────────────────────────────────┐  │    │
│  │  │ - event_queue  │  │          HeartbeatScheduler                  │  │    │
│  │  └──────┬─────────┘  │  (后台 asyncio.Task，每 N 秒 tick)          │  │    │
│  │         │            └─────────────────────────────────────────────┘  │    │
│  └─────────┼───────────────────────────────────────────────────────────┘    │
│            │                                                                │
│  ┌─────────┴───────────────────────────────────────────────────────────┐    │
│  │                     断连检测层                                        │    │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │    │
│  │  │ Transport层   │  │ 心跳超时     │  │  业务层 cancel token      │   │    │
│  │  │ write() 报错  │  │ (server→client)│ │ (RunContext取消传播)     │   │    │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘   │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                         ReAct Agent / LLM Provider                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                         BaseMemory / CheckpointStore                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 关键设计模式

| 模式 | 应用位置 | 理由 |
|---|---|---|
| **Manager + Worker** | ConnectionManager 全局管理，Connection 对象承载单连接状态 | 职责分离，Manager 管全局资源，Connection 管自身生命周期 |
| **Producer-Consumer** | 事件队列（Agent 产出 → 队列 → SSE 写出） | 解耦业务产出速度与网络发送速度，为背压控制提供介入点 |
| **优雅关闭（Graceful Shutdown）** | FastAPI lifespan 钩子 + asyncio.Event | 进程退出时有序关闭，不漏事件 |
| **CancellationToken 联动** | Connection.cancel() → RunContext.cancel() | 断连信号穿透到 Agent 执行链最深处 |
| **环形缓冲（Ring Buffer）** | Connection 内部事件队列 | 有界内存，慢客户端自动淘汰旧事件 |

### 3.3 核心组件清单

```
src/react_ai/streaming/
├── __init__.py
├── connection.py          # SSEConnection（单连接状态 + 事件队列 + 生命周期）
├── manager.py             # SSEConnectionManager（全局连接池 + 限流 + 清理任务）
├── heartbeat.py           # HeartbeatScheduler（心跳调度后台任务）
├── exceptions.py          # ConnectionClosedError / ClientGoneError / BackpressureError
└── settings.py            # streaming_settings（从全局 settings 派生）
```

---

## 4. 连接状态机

### 4.1 状态定义

```
                 ┌─────────────┐
                 │  CREATED    │  __init__()
                 └──────┬──────┘
                        │ .attach(writer)  # 绑定 HTTP Response
                        ▼
                 ┌─────────────┐
         ┌──────►│  ACTIVE     │◄──────────┐
         │       └──────┬──────┘           │
         │              │                  │
         │  超过 heartbeat_timeout         │ 客户端恢复
         │              ▼                  │
         │       ┌─────────────┐          │
         │       │  UNCERTAIN  │          │
         │       └──────┬──────┘          │
         │              │                  │
         │   心跳超时 + 探测失败           │ 探测成功
         │              ▼                  │
         │       ┌─────────────┐          │
         │       │  CLOSING    │          │
         │       └──────┬──────┘          │
         │              │                  │
         │    - flush 剩余事件             │
         │    - emit shutdown              │
         │              ▼                  │
         └───────┌─────────────┐          │
                 │  CLOSED     │          │
                 └─────────────┘          │
                                          │
                  客户端新心跳到达 ───────┘
```

### 4.2 状态转移触发条件

| 当前状态 | 触发事件 | 目标状态 | 关联动作 |
|---|---|---|---|
| CREATED | `attach(writer)` | ACTIVE | 写入 SSE headers，注册到 Manager |
| ACTIVE | Transport 层 write() 抛 BrokenPipeError | CLOSING | 标记 transport_gone=True，触发 cancel |
| ACTIVE | 心跳超时 + 探测 ACK 未收到 | UNCERTAIN | 发送探测心跳，等待 client pong |
| UNCERTAIN | 探测超时 | CLOSING | 同上 |
| UNCERTAIN | 探测成功（收到 pong） | ACTIVE | 重置 last_heartbeat |
| ACTIVE | 达到 buffer 上限（背压） | CLOSING | 标记 slow_consumer=True |
| 任意 | 收到 Manager.shutdown() 信号 | CLOSING | 发送 shutdown 事件，flush |
| CLOSING | flush 完成或超时 | CLOSED | 取消 Task，从 Registry 移除 |

### 4.3 状态数据结构

```python
class ConnectionState(Enum):
    CREATED = "created"
    ACTIVE = "active"
    UNCERTAIN = "uncertain"
    CLOSING = "closing"
    CLOSED = "closed"


class CloseReason(Enum):
    TRANSPORT_GONE = "transport_gone"       # TCP 层确认关闭
    HEARTBEAT_TIMEOUT = "heartbeat_timeout" # 心跳超时（3+ 周期无响应）
    BACKPRESSURE = "backpressure"           # 背压触发（慢客户端）
    CLIENT_GONE = "client_gone"             # GeneratorExit / ClientDisconnect
    MANAGER_SHUTDOWN = "manager_shutdown"   # 优雅停机
    TENANT_QUOTA_EXCEEDED = "tenant_quota"  # 新建时被拒（不算真实关闭）
    TIMEOUT_IDLE = "timeout_idle"           # 长时间无事件产出


@dataclass
class ConnectionInfo:
    conn_id: str
    tenant_id: str | None
    user_id: str | None
    session_id: str | None
    run_id: str | None
    created_at: float
    last_activity: float           # 最近一次事件写出或心跳
    last_heartbeat_sent: float
    last_heartbeat_ack: float
    state: ConnectionState
    close_reason: CloseReason | None
    events_sent: int
    events_dropped: int            # 背压丢弃计数
    bytes_sent: int
    buffer_size: int               # 当前排队事件数
```

---

## 5. 断连检测机制

### 5.1 三层检测模型（从快到慢）

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Transport 层即时检测（最快，毫秒级）            │
│                                                         │
│  FastAPI StreamingResponse 内部会将 async generator 的    │
│ 产出写入 ResponseStream 并 flush。当对端关闭时，          │
│ httpx.StreamWriter.drain() 或 write() 会立刻抛           │
│ BrokenPipeError / ClientDisconnect。                      │
│                                                         │
│  接入点：Connection 的 __anext__() 或 yield 包装层        │
│  动作：捕获异常 → cancel Agent Task → 记录 close_reason   │
├─────────────────────────────────────────────────────────┤
│  Layer 2: 心跳超时检测（秒级，覆盖静默断连）              │
│                                                         │
│  浏览器关闭标签页但未触发 TCP FIN（移动端常见）、          │
│  中间代理吞掉 FIN → 应用层无法感知。                       │
│                                                         │
│  机制：Connection 维护 last_heartbeat_ack 时间戳，       │
│ HeartbeatScheduler 定时检查，超过阈值标记 UNCERTAIN →     │
│ 发送探测心跳 → N 次探测 ACK 都超时 → CLOSING             │
│                                                         │
│  阈值：heartbeat_interval=15s, probe_count=3 → 45s 判定 │
├─────────────────────────────────────────────────────────┤
│  Layer 3: 业务层 CancellationToken 联动（兜底，保证穿透）  │
│                                                         │
│  如果 Agent 正在阻塞在 LLM HTTP 请求中，                  │
│ Transport 层断连信号无法穿透到 generator。                 │
│                                                         │
│  机制：Connection 持有 asyncio.Event 作为 cancel_token， │
│ Agent.stream() 内部的 RunContext.check_cancelled()       │
│ 定期检查。同时通过 asyncio.TaskGroup 把 connection.cancel │
│ 信号与 LLM 调用任务关联，断连时自动 asyncio.CancelledError│
│ 穿透到最深处的 await。                                    │
└─────────────────────────────────────────────────────────┘
```

### 5.2 Transport 层检测的正确姿势

FastAPI/Starlette 的 `StreamingResponse` 有个陷阱：**generator 中的 `yield` 不会立即写出**，要等到下一次 `__anext__` 才会 flush。但如果 Agent 正在 LLM 调用中（一次 yield 要等几秒甚至几十秒），断连信号在 transport 层积压，要等到下一个 yield 才能被感知。

解决方案：**在 generator 内部主动定期 drain**，配合心跳机制把断连检测窗口从「下一个事件产出」缩短到「心跳间隔」：

```python
async def _safe_stream(self, agent_gen, connection):
    """包装 Agent 原生 generator，注入断连检测 + 心跳调度 + 背压控制."""
    queue = connection.event_queue
    heartbeat_task = asyncio.create_task(connection.run_heartbeat())
    
    try:
        async for event in agent_gen:
            if connection.is_closed:
                break
            
            # 背压检查：队列满时非阻塞 put，满则标记慢客户端
            if not queue.full():
                queue.put_nowait(self._format_sse(event))
            else:
                connection.events_dropped += 1
                if connection.events_dropped >= self.max_drop_count:
                    connection.mark_closed(CloseReason.BACKPRESSURE)
                    break
            
            # 主动 drain + 断连探测
            await connection.drain()
            
    except (BrokenPipeError, ClientDisconnect):
        connection.mark_closed(CloseReason.TRANSPORT_GONE)
    except asyncio.CancelledError:
        connection.mark_closed(CloseReason.CLIENT_GONE)
        raise
    finally:
        heartbeat_task.cancel()
        # flush 队列中剩余事件（如果还没完全断）
        await connection.flush_remaining(timeout=2.0)
        connection.mark_closed(CloseReason.MANAGER_SHUTDOWN if not connection.is_closed else None)
```

### 5.3 CancellationToken 穿透链

Agent 当前有 `RunContext.check_cancelled()`，在 `_loop` 和 `_run_tool_calls` 中定期调用。需要把 Connection 的 cancel token 注入进去：

```python
class RunContext:
    def __init__(self, ...):
        # 新增：可选的外部取消信号
        self._external_cancel_event: asyncio.Event | None = None
    
    def bind_cancel_event(self, event: asyncio.Event):
        self._external_cancel_event = event
    
    def check_cancelled(self):
        # 原有本地标记检查
        if self._cancelled:
            raise asyncio.CancelledError()
        # 新增：外部事件检查（Connection 断连时触发）
        if self._external_cancel_event and self._external_cancel_event.is_set():
            raise asyncio.CancelledError()
```

### 5.4 断连检测时序图

```
客户端                    SSEConnection          Agent Task           HeartbeatScheduler
  │                          │                      │                       │
  │  关闭浏览器               │                      │                       │
  │──── TCP FIN ────────────►│                      │                       │
  │                          │  (gen yield 时)        │                       │
  │                          │  BrokenPipeError      │                       │
  │                          │                      │                       │
  │                          ├─ mark_closed()        │                       │
  │                          │                      │                       │
  │                          ├─ cancel_token.set()   │                       │
  │                          │                      │                       │
  │                          │                      │── check_cancelled() ──▶│
  │                          │                      │  → CancelledError     │
  │                          │                      │                       │
  │                          │                      │── Task 结束            │
  │                          │                      │                       │
  │                          │── remove_from_registry()                       │
  │                          │── emit CLOSED 指标   │                       │
  │                          │                      │                       │
```

---

## 6. 心跳保活策略

### 6.1 分层心跳（覆盖不同网络层级的空闲超时）

```
┌──────────────────────────────────────────────────────────┐
│  Layer 1: TCP keepalive（系统级，内核处理）                 │
│                                                          │
│  Uvicorn 默认开启，TCP_KEEPIDLE=60s, TCP_KEEPINTVL=10s   │
│  能穿透同网段 L4 负载均衡，对应用层完全透明。                │
│  但对 L7 代理（Nginx/ALB）的 HTTP idle timeout 无效。      │
├──────────────────────────────────────────────────────────┤
│  Layer 2: HTTP SSE 心跳（应用层，注释帧）                   │
│                                                          │
│  格式：SSE 协议允许以 ":" 开头的注释帧，会被客户端忽略，       │
│  但会强制 HTTP 层发送数据，刷新代理的 read timeout 计时器。  │
│                                                          │
│  内容：": heartbeat {ts}\n\n"                             │
│  频率：heartbeat_interval（默认 15s，与各代理 idle timeout  │
│        取值关系见 6.3 节）                                 │
│  实现：HeartbeatScheduler 后台 Task，遍历全部 ACTIVE        │
│        Connection，发送心跳帧 + 更新 last_heartbeat_sent   │
│                                                          │
│  注意：即使 Agent 正在执行工具调用（长时间无业务事件产出），│
│  心跳帧仍会持续产出，保证 HTTP 连接始终有流量。              │
├──────────────────────────────────────────────────────────┤
│  Layer 3: 应用层探测心跳（双向，检测真·死连接）              │
│                                                          │
│  浏览器 EventSource API 不支持主动回写，但可以借助          │
│  SSE event id + 客户端重连时的 Last-Event-ID 头来推断。    │
│                                                          │
│  简化方案：心跳帧携带递增序号，客户端（前端 SDK）需在       │
│  收到事件后主动 POST `/api/v1/connections/{conn_id}/pong`   │
│  确认。超过 probe_count × heartbeat_interval 无 pong →      │
│  判定连接真正不可达，进入 CLOSING。                          │
│                                                          │
│  如果客户端不支持 pong（向后兼容），退化为：连续            │
│  3 × heartbeat_interval 无任何业务事件产出 → 标记为疑似断连。│
└──────────────────────────────────────────────────────────┘
```

### 6.2 SSE 心跳帧格式

```
: heartbeat
id: 1733529600000

```

- `:` 开头 → SSE 协议层注释帧，客户端 EventSource 自动忽略
- `id:` 字段携带心跳序号，便于后续扩展客户端重连时的事件回放
- 空行 → SSE 协议的事件分隔符，强制 flush

### 6.3 心跳频率与代理超时的关系

| 代理层 | 典型 idle timeout | 建议心跳频率 |
|---|---|---|
| Nginx proxy_read_timeout | 60s | **15s**（≤ 1/4） |
| AWS ALB idle timeout | 300s | 30s |
| Cloudflare idle timeout | 100s | 25s |
| TCP keepalive（内核） | 60s | 60s |

**金规**：`heartbeat_interval ≤ min(所有路径上代理的 idle_timeout) / 3`

### 6.4 HeartbeatScheduler 实现要点

```python
class HeartbeatScheduler:
    """独立后台 asyncio.Task，遍历全部 ACTIVE Connection 发送心跳."""

    def __init__(self, manager: SSEConnectionManager, interval: float = 15.0, probe_timeout: float = 45.0):
        self._manager = manager
        self._interval = interval
        self._probe_timeout = probe_timeout
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run(), name="sse-heartbeat-scheduler")

    async def stop(self):
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            await self._task

    async def _run(self):
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.error("heartbeat_tick_error error=%s", e)
            await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)

    async def _tick(self):
        now = time.time()
        for conn in self._manager.iter_active():
            if conn.is_uncertain and (now - conn.last_heartbeat_ack) > self._probe_timeout:
                conn.mark_closed(CloseReason.HEARTBEAT_TIMEOUT)
                continue
            # 正常发心跳
            try:
                await conn.send_heartbeat(now)
            except Exception:
                conn.mark_closed(CloseReason.TRANSPORT_GONE)
```

---

## 7. 背压与缓冲区管理

### 7.1 问题：慢客户端吃空内存

Agent 产出事件速度（10-50 events/s）远快于弱网客户端消费速度。如果 Connection 内部无界排队，1000 条连接 × 10000 events × 1KB/event = ~10GB 内存，直接 OOM。

### 7.2 有界环形缓冲区

```python
class RingBuffer:
    """固定容量环形队列，满时丢弃最旧元素（非阻塞）."""
    
    def __init__(self, capacity: int):
        self._buf = [None] * capacity
        self._capacity = capacity
        self._head = 0      # 写入位置
        self._tail = 0      # 读取位置
        self._size = 0
        self._lock = asyncio.Lock()
        self._notify = asyncio.Event()

    async def put(self, item) -> bool:
        """放入队列，满时丢弃最旧元素并返回 False."""
        async with self._lock:
            if self._size >= self._capacity:
                # 丢弃最旧
                self._tail = (self._tail + 1) % self._capacity
                self._size -= 1
            self._buf[self._head] = item
            self._head = (self._head + 1) % self._capacity
            self._size += 1
            self._notify.set()
            return True

    async def get(self, timeout: float | None = None):
        """获取下一个元素，队列为空时等待."""
        if self._size == 0:
            self._notify.clear()
            if timeout is None:
                await self._notify.wait()
            else:
                await asyncio.wait_for(self._notify.wait(), timeout)
        async with self._lock:
            item = self._buf[self._tail]
            self._tail = (self._tail + 1) % self._capacity
            self._size -= 1
            if self._size > 0:
                self._notify.set()
            return item

    @property
    def size(self) -> int:
        return self._size
    
    @property
    def dropped(self) -> int:
        """累计被丢弃的元素数（需外部维护）."""
        return self._dropped
```

### 7.3 三级背压响应策略

| 等级 | 触发条件 | 动作 |
|---|---|---|
| L1 温和 | buffer 使用率 > 60% | 记录 WARN 日志 + Metrics 告警 |
| L2 强硬 | buffer 使用率 > 90% | 启用「仅保留关键事件」过滤：RUN_START / RUN_END / ERROR 保留，中间 TOOL_START/OBSERVATION 合并 |
| L3 熔断 | 累计丢弃事件 > 100 条 或 buffer 长期 100% | `mark_closed(CloseReason.BACKPRESSURE)` 主动断开该连接，记录 slow_consumer=true |

### 7.4 buffer 容量配置

```python
streaming:
  event_queue_capacity: 256       # 单连接事件队列容量（环形缓冲）
  max_events_per_connection: 5000 # 单连接生命周期事件上限（超过即强制关闭，防恶意长连接）
  backpressure_threshold_warn: 0.6
  backpressure_threshold_critical: 0.9
  slow_consumer_max_drops: 100    # L3 熔断阈值
```

---

## 8. 并发限流与资源配额

### 8.1 双重闸门

```
新建 SSE 连接请求
       │
       ▼
┌───────────────────────┐
│ 闸门 1: 全局并发上限    │ ── 超出 → 503 Service Unavailable
│ max_active_connections │
└───────────┬───────────┘
            │ 通过
            ▼
┌───────────────────────┐
│ 闸门 2: 租户级配额      │ ── 超出 → 429 Too Many Requests（带 Retry-After）
│ max_per_tenant         │
└───────────┬───────────┘
            │ 通过
            ▼
        建立连接
```

### 8.2 实现

```python
class ConnectionManager:
    def __init__(self, settings: StreamSettings):
        self._lock = asyncio.Lock()
        self._connections: dict[str, SSEConnection] = {}
        self._tenant_counts: dict[str, int] = {}
        self._settings = settings

    async def acquire_slot(self, tenant_id: str | None) -> None:
        """获取连接配额，无可用时抛 ResourceExhaustedError."""
        async with self._lock:
            total = len(self._connections)
            if total >= self._settings.max_active_connections:
                raise ResourceExhaustedError(
                    f"全局连接数 {total} 已达上限 {self._settings.max_active_connections}"
                )
            if tenant_id:
                tenant_count = self._tenant_counts.get(tenant_id, 0)
                if tenant_count >= self._settings.max_per_tenant:
                    raise ResourceExhaustedError(
                        f"租户 {tenant_id} 连接数 {tenant_count} 已达上限 {self._settings.max_per_tenant}"
                    )
                self._tenant_counts[tenant_id] = tenant_count + 1

    async def release_slot(self, tenant_id: str | None) -> None:
        async with self._lock:
            if tenant_id and self._tenant_counts.get(tenant_id, 0) > 0:
                self._tenant_counts[tenant_id] -= 1
                if self._tenant_counts[tenant_id] == 0:
                    self._tenant_counts.pop(tenant_id, None)
```

### 8.3 默认值与容量估算

| 配置项 | 默认值 | 估算依据 |
|---|---|---|
| `max_active_connections` | 2000 | 每连接约 1MB 内存（队列 + 状态），2000 ≈ 2GB，单实例保守值 |
| `max_per_tenant` | 200 | 租户级 DoS 防护，单租户 200 并发已覆盖绝大多数生产场景 |
| `max_idle_connections` | 500 | 心跳探测已判定不确定的连接上限 |
| `connection_timeout` | 300s | 单次 Agent 运行典型耗时上限，超过强制收口 |

---

## 9. 多租户隔离

### 9.1 隔离维度

| 维度 | 机制 | 说明 |
|---|---|---|
| 连接配额 | `_tenant_counts` 字典 | 按 tenant_id 独立计数，互不可达 |
| 指标标签 | Prometheus label `tenant_id` | 仪表盘按租户分组，异常可归因到具体租户 |
| 日志上下文 | 每条日志携带 tenant_id | 生产问题快速定位租户 |
| 僵尸清理 | 按租户分批扫描 | 避免单租户大量僵尸连接影响其他租户清理效率 |
| 优雅关闭 | 按租户优先级倒序关闭 | VIP 租户最后关闭 |

### 9.2 跨租户隔离硬闸

```python
async def remove_connection(self, conn_id: str, tenant_id: str | None = None):
    """移除连接，tenant_id 非空时强制匹配（防止越权删除）."""
    async with self._lock:
        conn = self._connections.get(conn_id)
        if conn is None:
            return
        if tenant_id is not None and conn.tenant_id != tenant_id:
            raise PermissionError(
                f"Attempted to remove connection {conn_id} belonging to tenant {conn.tenant_id},"
                f" but caller specified {tenant_id}"
            )
        del self._connections[conn_id]
```

---

## 10. 资源清理与内存防护

### 10.1 连接关闭时的清理链

```
Connection.mark_closed()
    │
    ├── 1. cancel_token.set()          ← 穿透到最深处的 Agent Task
    │
    ├── 2. queue.put_nowait(shutdown_event)  ← 通知消费侧收尾
    │
    ├── 3. flush_remaining(timeout=2s) ← 尽最大努力写出剩余事件
    │
    ├── 4. writer.drain()              ← 等待 TCP 缓冲区刷新
    │
    ├── 5. writer.close()              ← 关闭底层 transport
    │
    ├── 6. await writer.wait_closed()   ← 确认 TCP FIN 发送完成
    │
    ├── 7. Manager.release_slot()      ← 释放并发/租户配额
    │
    └── 8. 清空所有内部引用：
           - event_queue.clear()        ← RingBuffer 置空，释放排队事件
           - cancel_token = None        ← 打破对 Agent 的引用
           - task_handle = None         ← 打破对 asyncio.Task 的引用
           - payload_cache = None       ← 打破对事件数据的引用
```

### 10.2 定时僵尸清扫任务

```python
class ZombieReaper:
    """后台定时任务，扫描并清理可疑僵尸连接."""
    
    async def _reap(self):
        now = time.time()
        for conn in self._manager.iter_active():
            # 判定条件：长时间无活动 + 心跳已不确定
            if conn.is_uncertain and (now - conn.last_heartbeat_ack) > self._probe_timeout:
                conn.mark_closed(CloseReason.HEARTBEAT_TIMEOUT)
                continue
            # 判定条件：生命周期超过 connection_timeout 但还没正常结束
            if (now - conn.created_at) > self._settings.connection_timeout and conn.state == ACTIVE:
                conn.mark_closed(CloseReason.TIMEOUT_IDLE)

    async def run_forever(self):
        while True:
            await asyncio.sleep(60)  # 每分钟扫描
            await self._reap()
```

### 10.3 asyncio Task 泄漏防护

**铁律**：所有被 Connection 创建或关联的 asyncio Task，最终必须被 cancel + await。

```python
# Connection 创建的 Task 清单
self._heartbeat_task: asyncio.Task | None   # 心跳后台任务
self._agent_task: asyncio.Task | None       # Agent.stream() 的主任务（由 Manager 创建）

# 关闭时统一清理
async def _cleanup_tasks(self):
    for task in [self._heartbeat_task, self._agent_task]:
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
```

### 10.4 内存泄漏自查清单

| # | 检查项 | 验证方式 |
|---|---|---|
| 1 | 关闭的 Connection 对象被意外引用 | `python -m tracemalloc` + 检查 Connection 实例数 |
| 2 | RingBuffer 中的事件对象持有大量 payload | 事件序列化后只保留 SSE 字符串，原始 AgentEvent 及时置空 |
| 3 | asyncio.Task 未被 await 导致 warning | 运行时开启 `asyncio.get_event_loop().set_task_factory()` 或 Python 3.12+ 的 TaskGroup strict 模式 |
| 4 | Registry 字典中残留已关闭连接 | 每次 remove_connection 后 assert conn.state == CLOSED |
| 5 | HeartbeatScheduler 的任务列表残留 | 遍历 Scheduler 内部状态，确认每次 tick 后列表长度无增长 |

---

## 11. 可观测性集成

### 11.1 Prometheus 指标

新增到 `MetricsCollector` 的 SSE 连接维度指标：

```
# 连接生命周期计数
react_ai_sse_connections_total{state="created|active|closing|closed", tenant_id="t1"}        counter
react_ai_sse_connections_closed_total{reason="transport_gone|heartbeat_timeout|...", tenant_id="t1"}  counter
react_ai_sse_active_connections                                                              gauge
react_ai_sse_active_connections_by_tenant{tenant_id="t1"}                                    gauge

# 事件通道质量
react_ai_sse_events_sent_total{event_type="agent_started|tool_call|...", tenant_id="t1"}    counter
react_ai_sse_events_dropped_total{reason="backpressure|slow_client", tenant_id="t1"}        counter
react_ai_sse_heartbeat_sent_total{tenant_id="t1"}                                           counter
react_ai_sse_heartbeat_missed_total{tenant_id="t1"}                                         counter

# 资源占用
react_ai_sse_connection_duration_seconds{tenant_id="t1"}                                     histogram  (P50/P90/P99 连接寿命)
react_ai_sse_buffer_usage_ratio{tenant_id="t1"}                                              gauge       (当前全量连接平均 buffer 使用率)
react_ai_sse_memory_usage_bytes                                                              gauge       (ConnectionManager 估算内存占用)
```

### 11.2 实时诊断端点

```python
@router.get("/connections")
async def list_connections(
    tenant_id: str | None = None,
    limit: int = 100,
):
    """查看当前活跃连接列表，支持按租户筛选."""
    manager = get_connection_manager()
    return {
        "total_active": manager.active_count,
        "tenant_counts": manager.tenant_counts_snapshot(),
        "connections": [
            conn.to_public_info()
            for conn in manager.iter_active()
            if tenant_id is None or conn.tenant_id == tenant_id
        ][:limit],
    }

@router.get("/connections/{conn_id}")
async def get_connection_detail(conn_id: str):
    """单连接详细状态，含事件历史最近 20 条."""
    manager = get_connection_manager()
    conn = manager.get(conn_id)
    if conn is None:
        raise HTTPException(404)
    return conn.to_detail()
```

### 11.3 日志规范

| 事件 | 日志级别 | 结构化字段 |
|---|---|---|
| 连接建立 | INFO | conn_id, tenant_id, session_id, run_id |
| 事件写出 | DEBUG | conn_id, event_type, bytes |
| 心跳发送 | DEBUG | conn_id, heartbeat_seq |
| 心跳超时 | WARN | conn_id, missed_count, last_ack_ago |
| 背压告警 | WARN | conn_id, buffer_usage, dropped_count |
| 连接关闭 | INFO | conn_id, close_reason, duration_ms, events_sent |
| 连接被拒（限流） | WARN | tenant_id, current, limit |

### 11.4 分布式追踪

利用项目现有 Tracer（`src/react_ai/observability/tracer.py`），每个 Connection 创建独立 span：

```
trace_id = conn_id
span.start("sse.connection")
  → attrs: tenant_id, session_id, run_id
  → child span: sse.heartbeat (每 tick)
  → child span: sse.write (每次 drain)
  → child span: agent.stream (Agent 执行链)
span.end() → attrs: close_reason, duration_ms, events_sent
```

---

## 12. 降级与熔断

### 12.1 Connection Manager 自身熔断

极端情况下（如 heartbeat Task 异常、Registry 并发写冲突），Manager 需要能降级：

```python
class SSEConnectionManager:
    def __init__(self, ...):
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            name="sse_connection_manager",
        )
    
    async def acquire_slot(self, tenant_id):
        if not self._circuit_breaker.allow_request():
            # 熔断器打开，拒绝新连接
            raise ServiceUnavailableError("SSE connection manager circuit open")
        try:
            await self._do_acquire(tenant_id)
            self._circuit_breaker.record_success()
        except Exception as e:
            self._circuit_breaker.record_failure()
            raise
```

### 12.2 Agent 执行超时熔断

Connection 层面设置硬性执行超时，防止 Agent 在 LLM 慢响应时长时间占用：

```python
async def run_with_timeout(self, agent_gen, timeout: float | None = None):
    """将 Agent 执行包在超时保护中，超时自动取消."""
    timeout = timeout or self._settings.connection_timeout
    try:
        await asyncio.wait_for(self._safe_stream(agent_gen), timeout=timeout)
    except asyncio.TimeoutError:
        self.mark_closed(CloseReason.TIMEOUT_IDLE)
        logger.warning("connection_timeout conn_id=%s timeout=%.1fs", self.conn_id, timeout)
```

### 12.3 降级关闭（Graceful Degradation）

当服务器资源接近饱和（活跃连接 > 80% 上限），自动启用以下降级策略：

| 降级策略 | 触发条件 | 动作 |
|---|---|---|
| 关闭心跳探测 pong 要求 | 活跃连接 > 80% 上限 | 心跳仅发不要求 ACK，降低客户端回包压力 |
| 缩小事件队列容量 | 活跃连接 > 85% 上限 | 临时将新连接队列容量从 256 降到 128 |
| 拒绝非关键租户新连接 | 活跃连接 > 90% 上限 | 按租户优先级（配置化）选择性拒绝 |
| 关闭 DEBUG 级别事件日志 | 活跃连接 > 95% 上限 | 减少 I/O 压力 |

---

## 13. API 设计

### 13.1 FastAPI lifespan 集成

```python
# api/main.py
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # === Startup ===
    manager = get_connection_manager()
    await manager.start()  # 启动 HeartbeatScheduler + ZombieReaper
    app.state.connection_manager = manager
    
    yield
    
    # === Shutdown ===
    logger.info("sse_manager_shutting_down active_connections=%d", manager.active_count)
    await manager.shutdown(timeout=5.0)  # 5s 优雅关闭窗口
    logger.info("sse_manager_shutdown_complete")
```

### 13.2 路由层调用方式（透明集成）

```python
# api/routes/agent.py
@router.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, agent: ReActAgent = Depends(get_agent)):
    manager = get_connection_manager()
    
    # 1. 资源闸门（无可用抛 503/429）
    await manager.acquire_slot(tenant_id=req.tenant_id)
    
    # 2. 创建 Connection 并绑定事件队列
    connection = manager.create_connection(
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        session_id=req.session_id,
    )
    
    ctx = _build_context(req)
    ctx.bind_cancel_event(connection.cancel_event)  # 注入外部取消信号
    
    # 3. 创建 Agent Task
    agent_task = asyncio.create_task(agent.stream(req.query, ctx))
    
    async def event_generator():
        try:
            async for sse_line in connection.run_stream(agent_task):
                yield sse_line
        finally:
            manager.remove_connection(connection.conn_id)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

### 13.3 Connection 核心接口

```python
class SSEConnection:
    conn_id: str
    state: ConnectionState
    is_closed: bool
    cancel_event: asyncio.Event
    event_queue: RingBuffer
    
    async def run_stream(self, agent_task: asyncio.Task) -> AsyncGenerator[str, None]:
        """核心方法：消费 agent_task 产出的事件，封装为 SSE 协议格式并输出."""
    
    async def run_heartbeat(self) -> None:
        """心跳循环（由 HeartbeatScheduler 调度 or 独立 Task）."""
    
    async def send_heartbeat(self, seq: int) -> None:
        """发送一条心跳帧."""
    
    async def drain(self) -> None:
        """刷新 TCP 缓冲区，顺便探测 transport 层健康."""
    
    def mark_closed(self, reason: CloseReason) -> None:
        """标记关闭状态，触发 cancel 信号传播."""
    
    def to_public_info(self) -> dict:
        """暴露给 /connections 端点的脱敏状态."""
```

### 13.4 ConnectionManager 核心接口

```python
class SSEConnectionManager:
    async def start(self) -> None                          # 启动心跳/清理后台任务
    async def shutdown(self, timeout: float = 5.0) -> None # 优雅关闭全部连接
    
    async def acquire_slot(self, tenant_id: str | None) -> None
    async def release_slot(self, tenant_id: str | None) -> None
    
    def create_connection(self, ...) -> SSEConnection
    async def remove_connection(self, conn_id: str) -> None
    
    def get(self, conn_id: str) -> SSEConnection | None
    def iter_active(self) -> Iterator[SSEConnection]
    
    @property
    def active_count(self) -> int
    @property
    def tenant_counts_snapshot(self) -> dict[str, int]
    
    def get_stats(self) -> dict                            # 供 /metrics/snapshot 集成
```

---

## 14. 与现有代码集成方案

### 14.1 需要改动的文件清单

| 文件 | 改动点 | 行数估计 |
|---|---|---|
| `api/main.py` | 替换为 lifespan 启动模式 | ~5 行 |
| `api/routes/agent.py` | 路由接入 ConnectionManager | ~15 行 |
| `src/react_ai/core/context.py` | RunContext 新增 `bind_cancel_event` | ~8 行 |
| `src/react_ai/observability/metrics.py` | 新增 SSE 连接维度指标 | ~50 行 |
| `pyproject.toml` | 可能新增 asyncio 相关依赖（无，纯标准库 + FastAPI） | 0 行 |

### 14.2 新增文件清单

| 文件 | 说明 | 行数估计 |
|---|---|---|
| `src/react_ai/streaming/__init__.py` | 导出公共 API | ~10 行 |
| `src/react_ai/streaming/connection.py` | SSEConnection + RingBuffer | ~250 行 |
| `src/react_ai/streaming/manager.py` | SSEConnectionManager | ~200 行 |
| `src/react_ai/streaming/heartbeat.py` | HeartbeatScheduler + ZombieReaper | ~120 行 |
| `src/react_ai/streaming/exceptions.py` | 自定义异常 | ~20 行 |
| `src/react_ai/config/settings.py` | 新增 `streaming` 配置组 | ~25 行 |

### 14.3 向后兼容性

| 场景 | 兼容性保证 |
|---|---|
| 客户端不支持 pong 回包 | Connection 会退化为「仅靠 transport + 长时间无活动判定」 |
| 无 HeartbeatScheduler 时 | Connection 可独立运行（心跳由 Connection 内部定时任务） |
| Manager acquire_slot 抛 503/429 | 返回标准 HTTP 状态码，客户端可直接重试 |
| 关闭现有调用方（旧的裸 StreamingResponse） | ConnectionManager 为可选接入，老代码不改也能跑 |

---

## 15. 配置项一览

```python
# src/react_ai/config/settings.py 新增

class StreamSettings(BaseModel):
    """SSE 连接管理器配置."""
    
    # === 并发控制 ===
    max_active_connections: int = 2000          # 单进程最大活跃 SSE 连接数
    max_per_tenant: int = 200                    # 单租户最大并发连接数
    connection_timeout: float = 300.0            # 单连接最大存活时间（秒）
    
    # === 心跳配置 ===
    heartbeat_interval: float = 15.0             # 心跳发送间隔（秒）
    heartbeat_probe_count: int = 3               # 连续多少次探测无响应判定断连
    heartbeat_require_pong: bool = False         # 是否要求客户端主动 pong 回包
    
    # === 背压配置 ===
    event_queue_capacity: int = 256              # 单连接环形缓冲区容量
    slow_consumer_max_drops: int = 100           # 超过该值强制断开慢客户端
    backpressure_warn_ratio: float = 0.6
    backpressure_critical_ratio: float = 0.9
    
    # === 资源清理 ===
    zombie_reaper_interval: float = 60.0         # 僵尸扫描间隔（秒）
    shutdown_grace_period: float = 5.0           # 优雅停机等待窗口（秒）
    
    # === 降级阈值 ===
    circuit_breaker_failure_threshold: int = 5   # 熔断器打开阈值
    circuit_breaker_recovery: float = 30.0       # 熔断器恢复时间（秒）
    degrade_at_ratio: float = 0.8                # 达到上限 80% 启用降级策略
    
    # === 缓冲区刷新 ===
    drain_interval: float = 0.5                  # 强制 drain 间隔（秒），配合心跳
    tcp_keepalive: bool = True                   # 启用 TCP keepalive
```

---

## 16. 测试策略

### 16.1 单元测试（tests/unit/test_sse_connection.py）

| 测试场景 | 验证点 |
|---|---|
| Connection 创建与销毁 | 创建 → ACTIVE → CLOSED，引用计数归零 |
| Transport 断连检测 | 模拟 BrokenPipeError → Connection 状态变为 CLOSING → CLOSED |
| 心跳超时检测 | mock `time.sleep` 跳过心跳周期 → Connection 状态变为 UNCERTAIN → CLOSED |
| 背压触发 | 填满 RingBuffer 持续 put → L1/WARN → L2 过滤 → L3 熔断 |
| CancelEvent 穿透 | Connection.cancel() → RunContext.check_cancelled() 抛 CancelledError → Agent Task 结束 |
| 资源清理链 | mark_closed() 后所有引用置空，tracemalloc 验证无泄漏 |

### 16.2 集成测试（tests/integration/test_sse_streaming.py）

| 测试场景 | 验证点 |
|---|---|
| FastAPI lifespan start/shutdown | 启动后 HeartbeatScheduler 运行，shutdown 后全部连接关闭 |
| HTTP 层断连 | httpx.AsyncClient 发起 SSE 请求 → 中途 close() → 验证 Connection 被清理 |
| 限流闸门 | 并发创建 max_active_connections + 1 个连接 → 第 N+1 个抛 ResourceExhaustedError |
| 多租户隔离 | tenant_A 建满配额 → tenant_B 仍可正常建连接 |
| 优雅停机 | 发送 SIGTERM → 3s 内全部连接 CLOSED，无 orphan task |

### 16.3 压测与稳定性测试

| 测试场景 | 验证点 |
|---|---|
| 2000 并发 SSE 连接 + 随机断连 | 内存占用稳定（≤ 2GB），CPU 无异常飙升 |
| 慢客户端模拟（1KB/s 消费速度） | 背压机制正常触发，慢客户端被断开，其他连接不受影响 |
| 长时间运行（24h） | Connection 实例数不增长（无泄漏），metrics 中 created - closed = 0 |
| 优雅停机期间新连接涌入 | shutdown 阶段新连接请求返回 503，已在关闭中的连接正常收口 |

---

## 17. 对标大厂方案对比

| 维度 | react-ai 方案 | Spring Boot SseEmitter | Node.js Socket.IO | Go-Kratos ConnectionPool | 说明 |
|---|---|---|---|---|---|
| **连接管理粒度** | 单连接对象独立状态 | SseEmitter 单例无外部状态管理 | Socket 连接池 + 房间 | Connection 对象 + Manager | react-ai 接近 Kratos |
| **断连检测** | 三层（Transport + 心跳 + CancelToken） | 仅依赖 AsyncRequestNotUsableException | 心跳 + WebSocket ping/pong | 心跳 + 读写超时 | react-ai 最完整 |
| **背压保护** | 有界环形缓冲 + 三级熔断 | 无，依赖 Spring 内部 buffer | 无，依赖 Node.js backpressure | 读写分离 + 缓冲队列 | react-ai 有明确策略 |
| **并发限流** | 全局 + 租户级双重闸门 | 无（需外部 Filter） | 无（需中间件） | 连接池容量控制 | react-ai 原生内置 |
| **优雅停机** | FastAPI lifespan + 超时窗口 | ContextRefreshedEvent | SIGTERM handler | ctx.Done() + drain | react-ai 标准集成 |
| **可观测性** | Prometheus + tracer span | 无内置（Micrometer 需手动埋点） | 无内置 | 内置 metrics | react-ai 最完整 |
| **多租户** | 配额 + 指标 + 日志全隔离 | 无 | 无 | 无 | react-ai 独有 |

---

## 18. 落地路线图

### Phase 1: MVP（核心连接管理）— 预计 3 天

- [ ] `streaming/connection.py`：SSEConnection + RingBuffer 实现
- [ ] `streaming/manager.py`：基础 Registry + acquire/release slot
- [ ] RunContext.bind_cancel_event 改造
- [ ] 路由层接入 ConnectionManager（最小改动）
- [ ] 单元测试：断连检测、资源清理、基本生命周期

### Phase 2: 心跳 + 背压 — 预计 2 天

- [ ] HeartbeatScheduler 后台任务
- [ ] ZombieReaper 僵尸清理
- [ ] 背压三级熔断
- [ ] 配置项全量落地
- [ ] 单元测试：心跳超时、背压触发、僵尸清扫

### Phase 3: 可观测 + 优雅停机 — 预计 2 天

- [ ] MetricsCollector 新增 SSE 连接维度指标
- [ ] FastAPI lifespan 集成
- [ ] `/connections` 诊断端点
- [ ] 集成测试：限流闸门、优雅停机、多租户隔离
- [ ] 压测验证

### Phase 4: 降级策略 + 容错 — 预计 1 天

- [ ] ConnectionManager 熔断器
- [ ] 资源饱和降级策略
- [ ] 文档完善

---

## 附录 A：关键代码模板

### A.1 SSEConnection 伪代码骨架

```python
class SSEConnection:
    def __init__(self, conn_id, tenant_id, user_id, session_id, settings):
        self.conn_id = conn_id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.session_id = session_id
        self.settings = settings
        
        # 状态
        self.state = ConnectionState.CREATED
        self.close_reason = None
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.last_heartbeat_sent = 0.0
        self.last_heartbeat_ack = 0.0
        
        # 通道
        self.event_queue = RingBuffer(capacity=settings.event_queue_capacity)
        self.cancel_event = asyncio.Event()
        self._agent_task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._heartbeat_task: asyncio.Task | None = None
        
        # 统计
        self.events_sent = 0
        self.events_dropped = 0
        self.bytes_sent = 0
    
    @property
    def is_closed(self) -> bool:
        return self.state in (ConnectionState.CLOSING, ConnectionState.CLOSED)
    
    async def attach(self, writer: asyncio.StreamWriter) -> None:
        """绑定 HTTP Response writer，写入 SSE headers."""
        self._writer = writer
        self.state = ConnectionState.ACTIVE
    
    async def run_stream(self, agent_task: asyncio.Task) -> AsyncGenerator[str, None]:
        """主循环：从 event_queue 消费事件并输出 SSE 帧."""
        self._agent_task = agent_task
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        
        try:
            while not self.is_closed:
                # 非阻塞读取事件，有超时以便及时响应关闭信号
                try:
                    sse_line = await asyncio.wait_for(
                        self.event_queue.get(),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    # 队列空闲，检查心跳是否该发了
                    continue
                
                if sse_line is None:  # 哨兵值表示关闭
                    break
                
                try:
                    # 写出 + drain，这里是断连检测的关键点
                    self._writer.write(sse_line.encode())
                    await asyncio.wait_for(self._writer.drain(), timeout=5.0)
                    self.bytes_sent += len(sse_line)
                    self.events_sent += 1
                    self.last_activity = time.time()
                    yield sse_line
                except (BrokenPipeError, ConnectionResetError):
                    self.mark_closed(CloseReason.TRANSPORT_GONE)
                    break
        
        except asyncio.CancelledError:
            self.mark_closed(CloseReason.CLIENT_GONE)
            raise
        finally:
            await self._cleanup()
    
    async def _heartbeat_loop(self) -> None:
        """心跳循环：按 heartbeat_interval 发送注释帧."""
        seq = 0
        while not self.is_closed:
            await asyncio.sleep(self.settings.heartbeat_interval)
            if self.is_closed or self.state == ConnectionState.UNCERTAIN:
                continue
            try:
                hb = f": heartbeat {seq}\nid: {seq}\n\n"
                self._writer.write(hb.encode())
                await self._writer.drain()
                self.last_heartbeat_sent = time.time()
                seq += 1
            except Exception:
                self.mark_closed(CloseReason.TRANSPORT_GONE)
                return
    
    def mark_closed(self, reason: CloseReason) -> None:
        if self.is_closed:
            return
        self.close_reason = reason
        self.state = ConnectionState.CLOSING
        self.cancel_event.set()  # 触发 RunContext.check_cancelled()
    
    async def _cleanup(self) -> None:
        self.state = ConnectionState.CLOSED
        
        # 取消所有子 Task
        for task in [self._heartbeat_task, self._agent_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        
        # 关闭 writer
        if self._writer:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=2.0)
            except Exception:
                pass
        
        # 清空队列释放内存
        self.event_queue.clear()
        
        # 引用置空
        self._writer = None
        self._heartbeat_task = None
        self._agent_task = None
```

---

*文档版本：v1.0 | 2026-11-06 | 适用于 react-ai 项目 SSE 流式输出场景*