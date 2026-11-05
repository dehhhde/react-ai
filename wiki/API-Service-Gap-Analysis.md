# API 服务层企业级差距分析

> 适用对象：[api/](file:///d:/code/react-ai/api/) 目录（FastAPI REST + SSE 服务）及其安全、运维、多租户能力。
> 本文档回答：**当前 API 服务层到了什么水准？距离企业级生产微服务网关还差什么、怎么补？**
> 更新时间：2026-11-04（已按代码现状核对各项落地状态）

---

## 1. 当前架构速览

```text
                     ┌────────────────────────────────────────────┐
   客户端 ──HTTP──►  │  FastAPI (api/main.py)                      │
                     │  CORS: allow_origins=["*"] + credentials   │ ← ⚠️ 矛盾且危险
                     │  无任何认证依赖 / 无限流中间件                │
                     └────────────────────────────────────────────┘
                          │
        ┌─────────────────┼──────────────────────┐
        ▼                 ▼                      ▼
  /api/v1/agent/*   /api/v1/approvals/*    /api/v1/observability/*
  chat / stream     list / decision        metrics / audit / spans
  tools / resume    ⚠️ 匿名可审批!          ⚠️ 匿名可看审计!
        │                 │
        ▼                 ▼
  ReActAgent        ApprovalService          ⚠️ 全链路无 tenant 身份，
 (单例共享)          (进程内存态)                 无全局异常处理器
```

### 已实现能力清单

| 能力 | 实现位置 | 说明 |
|---|---|---|
| REST 同步调用 | [api/routes/agent.py#L86-L90](file:///d:/code/react-ai/api/routes/agent.py#L86-L90) `POST /chat` | 走 `agent.run()` 返回完整响应 |
| SSE 流式端点 | [api/routes/agent.py#L93-L102](file:///d:/code/react-ai/api/routes/agent.py#L93-L102) `POST /chat/stream` | 事件 `model_dump_json()` 逐条下发 |
| 审批恢复端点 | [api/routes/agent.py#L110-L120](file:///d:/code/react-ai/api/routes/agent.py#L110-L120) `POST /runs/{run_id}/resume` | 校验 checkpoint 存在后 SSE 恢复 |
| 审批列表/决策 | [api/routes/approvals.py#L19-L60](file:///d:/code/react-ai/api/routes/approvals.py#L19-L60) | 状态校验、422/404/409 业务错误码 |
| 可观测出口 | [api/routes/observability.py#L12-L41](file:///d:/code/react-ai/api/routes/observability.py#L12-L41) | `/metrics`(Prometheus 文本)、`/metrics/snapshot`、`/audit`、`/spans` |
| 依赖注入可测性 | [api/routes/agent.py#L55-L77](file:///d:/code/react-ai/api/routes/agent.py#L55-L77) | 懒构建单例，`dependency_overrides` 可替换 |
| 健康检查 | [api/main.py#L27-L29](file:///d:/code/react-ai/api/main.py#L27-L29) `GET /health` | 静态 `{status: ok}` |
| CORS | [api/main.py#L14-L20](file:///d:/code/react-ai/api/main.py#L14-L20) | 全开 |

**结论：端点覆盖与错误码语义是干净的，但整个服务处于"裸奔"状态——无认证、无限流、无租户、无异常兜底，任何能触达端口的人都可以驱动 LLM、批准高危工具调用、拉取全量审计。按生产标准衡量存在下述 8 项差距。**

---

## 2. 企业级差距分析（按重要性排序）

### 2.1 零认证 —— 任何人可批准高危操作 🔒 P0 ❌ 未开始

**现状**：grep 全项目 `api/` 无 `HTTPBearer` / `OAuth2` / `Depends(鉴权)` / `api_key` 任何形态。所有端点匿名可用，包括：
- `POST /approvals/{request_id}/decision`（[approvals.py#L50-L65](file:///d:/code/react-ai/api/routes/approvals.py#L50-L65)）——**任何人可替企业批准退款、拦截发货等高危操作**，HITL 安全机制在服务边界被完全绕过；
- `POST /chat`——任何人免费消耗 LLM token；
- `GET /audit`、`/spans`——任何人拉取含业务数据的审计记录。

**风险**：审批闸门形同虚设（攻击者可自行批准自行触发的挂起）；审计数据泄露；这是所有差距中唯一"直接抵消框架核心安全设计"的一项。

**企业级做法**：
1. 全局认证依赖：`app = FastAPI(dependencies=[Depends(require_auth)])`，实现 API Key（服务间）+ JWT（控制台用户）双模式；
2. 审批决策端点额外要求 `approver` 身份（与申请人不同，见 Approval-Checkpoint-Gap-Analysis 2.2）；
3. 可观测端点归入独立 `admin` 权限组。

**验收标准**：无凭证访问任意端点返回 401；决策端点校验审批人身份并写入审计链；测试覆盖未授权路径。

---

### 2.2 CORS 通配符 + credentials 矛盾组合 🌐 P0 ❌ 未开始

**现状**：[api/main.py#L14-L20](file:///d:/code/react-ai/api/main.py#L14-L20) 同时设置 `allow_origins=["*"]` 与 `allow_credentials=True`。按 CORS 规范该组合对携带凭证的请求无效（浏览器拒绝），但实际效果是：不带凭证的跨域请求全放行，配置意图（允许凭证）与实际行为（静默失效）背离，且一旦 2.1 落地加了 Cookie/Token 认证，开发者大概率会"顺手"把 origins 保持 `*` 来"修好"报错。

**风险**：安全配置靠猜；引入认证后极易演变成真实的跨站凭证滥用。

**企业级做法**： origins 白名单进配置（`CORS_ORIGINS` 环境变量，逗号分隔），生产默认不含 `*`；`allow_credentials` 仅在白名单为具体域名时开启；启动时校验矛盾组合并拒绝。

**验收标准**：`CORS_ORIGINS` 可配置且默认安全；`*` + credentials 组合启动即报错；预检请求行为有测试覆盖。

---

### 2.3 无入口级限流与配额 💥 P0 ❌ 未开始

**现状**：grep `api/` 无 `rate_limit` / `slowapi` / `throttle` 任何命中。`settings.security.enable_rate_limit`（[config/settings.py#L56](file:///d:/code/react-ai/src/react_ai/config/settings.py#L56)）只在**工具沙箱层**生效，管不住 `/chat` 入口——每次 chat 请求至少触发一次 LLM 调用，工具限流对纯对话消耗零约束。

**风险**：单客户端可无限并发打满 LLM 配额（成本 DoS）；SSE 长连接无并发上限，进程资源可被耗尽。

**企业级做法**：
1. 入口限流中间件：按 `tenant_id + 端点` 维度 RPM 限制，返回 429 + `Retry-After`；
2. 并发 SSE 连接数上限；
3. 与 LLM 网关配额联动（见 LLM-Gateway-Gap-Analysis 2.5），入口限流值 ≤ 下游模型配额。

**验收标准**：超阈值请求返回 429 且带 `Retry-After`；限流按租户隔离不误伤他人；`/metrics` 可见限流拒绝计数。

---

### 2.4 无租户身份链路 🏢 P1 ❌ 未开始

**现状**：`ChatRequest`（[agent.py#L80-L83](file:///d:/code/react-ai/api/routes/agent.py#L80-L83)）只有 `query/session_id/stream` 三字段；`RunContext` 缺省 `"default-session"`。认证缺失（2.1）导致没有 tenant/user 身份来源，工具侧虽设计了 `ToolContext` 注入租户（[agent.py#L66-L68](file:///d:/code/react-ai/api/routes/agent.py#L66-L68) 注释），但服务层无人可注入。

**风险**：Agent 单例跨请求共享（[agent.py#L66-L77](file:///d:/code/react-ai/api/routes/agent.py#L66-L77)）的前提是"工具不持有租户态"，而租户态实际无处注入——多租户数据隔离（售后订单查询等工具）在 API 层断裂；记忆/审计的 tenant 维度全部落空。

**企业级做法**：JWT/API Key 中解析 `tenant_id`，依赖注入生成请求级身份对象，向下透传到 `RunContext`、`ToolContext`、memory 查询、审计记录（是 2.1、Memory 2.2/2.6、Approval 2.2 的公共前置）。

**验收标准**：审计与指标记录可按 tenant 过滤；跨租户资源（run_id、approval_id）访问返回 404。

---

### 2.5 无全局异常处理与统一错误契约 🧯 P1 ❌ 未开始

**现状**：[api/main.py](file:///d:/code/react-ai/api/main.py) 无 `@app.exception_handler`；除 `HTTPException` 外的未捕获异常将由 FastAPI 抛 500，响应含内部错误细节；`approvals.py` 手写 404/409/422 的 `detail` 字符串，各端点错误格式不一致，无错误码字典。

**风险**：堆栈/内部信息经响应泄露；客户端无法程序化处理错误；SSE 生成器内异常直接断流无终态事件。

**企业级做法**：注册全局 handler：`Exception` → 统一 `{code, message, trace_id}` 契约（trace_id 关联 OTel），脱敏堆栈只进日志；SSE 生成器 try/except 包裹，异常时下发 `RUN_ERROR` 终态事件再关流。

**验收标准**：任意端点异常返回统一错误体且不含堆栈；SSE 异常路径有 `RUN_ERROR` 事件；错误码有文档化枚举。

---

### 2.6 SSE 无心跳与断线语义 📡 P1 ❌ 未开始

**现状**：[agent.py#L97-L102](file:///d:/code/react-ai/api/routes/agent.py#L97-L102) 事件流只 `yield data:`，无周期心跳注释帧（`: ping`）、无 `id:` 字段、不处理 `Last-Event-ID` 请求头；LLM 长思考（>网关空闲超时）期间连接可能被中间层静默掐断。

**风险**：企业网关/负载均衡常见 60s 空闲断连导致流丢失且客户端无感知；断线后无法续传，只能整单重跑（可能重复触发副作用工具）。

**企业级做法**：每 15-20s 下发心跳注释；事件带自增 `id:`；客户端重连携带 `Last-Event-ID` 时从 checkpoint/事件缓冲续发（与 resume 端点语义打通）。

**验收标准**：抓包可见周期心跳；断开重连后事件不重复不丢失（至少覆盖挂起-恢复场景）。

---

### 2.7 请求体无约束 📏 P2 ❌ 未开始

**现状**：`query: str` 无 `max_length`；无请求体大小限制；无幂等键（`Idempotency-Key` 头）——`/chat` 重试会重复执行整条 ReAct 链（工具层幂等只防同调用重复，不防整 run 重复）。

**企业级做法**：Pydantic `Field(max_length=...)`；中间件限制 body 大小；`/chat` 支持 `Idempotency-Key`，同键短窗口内返回首个结果。

**验收标准**：超长 query 返回 422；同幂等键并发请求只产生一次执行。

---

### 2.8 健康检查与运行模式不生产 🩺 P2 ❌ 未开始

**现状**：`/health` 静态返回 ok（[main.py#L27-L29](file:///d:/code/react-ai/api/main.py#L27-L29)），不探测 LLM 可达性、Redis/PG、审计后端健康；`__main__` 入口 `reload=True`（[main.py#L34](file:///d:/code/react-ai/api/main.py#L34)）易被误用于生产。

**企业级做法**：拆分 `/healthz`（存活，静态）与 `/readyz`（就绪，探测依赖，含降级状态）；README/部署文档明确生产用 uvicorn worker 模式启动。

**验收标准**：依赖后端故障时 `/readyz` 返回非 200 并列出故障项。

---

## 3. 实施优先级路线

| 级别 | 事项 | 排序依据 |
|---|---|---|
| **P0（阻塞上线）** | 2.1 认证 → 2.3 入口限流 → 2.2 CORS 白名单 | 匿名可审批直接抵消 HITL 设计；成本 DoS 是钱包红线；2.1 是 2.4 的前置 |
| **P1（多租户 / 稳定）** | 2.4 租户链路 → 2.5 错误契约 → 2.6 SSE 心跳续传 | 租户是记忆/审批/审计隔离的公共地基；SSE 稳定性决定长任务可用性 |
| **P2（健壮性）** | 2.7 请求约束/幂等 → 2.8 就绪探针 | 锦上添花，依赖 P0/P1 身份与错误基建 |

> **落地核对（2026-11-04）**：8 项全部未开始。
> 建议把 **2.4 租户身份链路**作为第一刀（与 2.1 认证同一次改动完成），因为 Memory 2.2/2.6、Approval 2.2、LLM 网关 2.4 的成本归因全部依赖它。

---

## 4. 相关代码与文档索引

- 服务入口：[api/main.py](file:///d:/code/react-ai/api/main.py) · 路由：[api/routes/agent.py](file:///d:/code/react-ai/api/routes/agent.py) · [api/routes/approvals.py](file:///d:/code/react-ai/api/routes/approvals.py) · [api/routes/observability.py](file:///d:/code/react-ai/api/routes/observability.py)
- 配置：[config/settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py) `SecurityConfig`（`ENABLE_RATE_LIMIT` 仅工具层生效）
- 关联模块：审批链 [approval/service.py](file:///d:/code/react-ai/src/react_ai/approval/service.py) · 上下文 [core/context.py](file:///d:/code/react-ai/src/react_ai/core/context.py) · 幂等 [idempotency/guard.py](file:///d:/code/react-ai/src/react_ai/idempotency/guard.py)
- 参照文档：[Prompts-Gap-Analysis.md](file:///d:/code/react-ai/wiki/Prompts-Gap-Analysis.md)（格式参照） · [API-Service.md](file:///d:/code/react-ai/wiki/API-Service.md) · [Memory-Gap-Analysis.md](file:///d:/code/react-ai/wiki/Memory-Gap-Analysis.md)（租户链路依赖） · [Approval-Checkpoint-Gap-Analysis.md](file:///d:/code/react-ai/wiki/Approval-Checkpoint-Gap-Analysis.md)（匿名审批风险）
