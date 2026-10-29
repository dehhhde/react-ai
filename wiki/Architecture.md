# Architecture 架构总览

> 本文是 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) 的 Wiki 版摘要，配合各模块页面阅读。

## 设计理念

1. **契约先行 (Contract-First)**：消息、事件、工具声明、模型出入参全部使用 Pydantic V2 强类型（`react_ai.schemas`）。
2. **ReAct 闭环可控**：`Thought → Action → Observation` 状态机，内置最大步数、超时熔断与 Token 预算，杜绝死循环。
3. **防腐层隔离**：LLM 厂商差异、存储后端差异均通过抽象基类隔离（`BaseLLMClient`、`BaseMessageStore`）。
4. **安全默认**：工具沙箱超时、高危指令黑名单、护栏阻断为默认行为。
5. **横切关注点独立**：可观测性、提示词、配置作为跨层基础设施。

## 分层拓扑

```text
+---------------------------------------------------+
|        Client Layer (Web / Chat / SDK)            |
+---------------------------------------------------+
                     | HTTP / SSE Stream
                     v
+---------------------------------------------------------------+
| API Serving Layer (api/)                                      |
|  POST /api/v1/agent/chat | /chat/stream (SSE) | GET /tools    |
|  Middlewares: CORS, /health 探针                              |
+---------------------------------------------------------------+
                     |
                     v
+---------------------------------------------------------------+
| Core Runtime & State Engine (react_ai/core/)                  |
|  BaseAgent / ReActAgent / RunContext / AgentState / Hooks     |
+---------------------------------------------------------------+
     |            |             |              |
     v            v             v              v
+-----------+ +-----------+ +-----------+ +--------------+
| LLM       | | Tools     | | Memory    | | Guardrails   |
| Gateway   | | Ecosystem | | Hierarchy | | Security     |
| (llm/)    | | (tools/)  | | (memory/) | | (guardrails/)|
+-----------+ +-----------+ +-----------+ +--------------+
                     |
                     v
+---------------------------------------------------------------+
| Cross-Cutting Concerns                                        |
|  observability/ | prompts/ | config/ | schemas/ | utils/      |
+---------------------------------------------------------------+
```

## 包职责一览

| 包 | 层次 | 核心类型 | Wiki 页面 |
|---|---|---|---|
| `react_ai.core` | 运行时引擎 | `ReActAgent`、`RunContext`、`AgentState`、`AgentLifecycleHook` | [Core Engine](Core-Engine.md) |
| `react_ai.schemas` | 数据契约 | `ChatMessage`、`AgentEvent`、`ToolDefinition`、`AgentConfig` | [Core Engine](Core-Engine.md) |
| `react_ai.llm` | 模型网关 | `BaseLLMClient`、`LLMRouter`、`create_llm_client`、Providers | [LLM Gateway](LLM-Gateway.md) |
| `react_ai.tools` | 工具生态 | `@tool`、`ToolRegistry`、`ToolSandbox`、内置/售后工具 | [Tools](Tools.md) |
| `react_ai.memory` | 记忆层 | `BaseMemory`、`WindowBufferMemory`、`LongTermSemanticMemory`、Stores | [Memory](Memory.md) |
| `react_ai.orchestration` / `flows` | 编排层 | `BaseOrchestrator`、`SupervisorAgent`、`AgentRouter`、`StateGraphWorkflow`、`BaseFlow` | [Orchestration](Orchestration.md) |
| `react_ai.guardrails` | 安全护栏 | `BaseGuardrail`、`GuardrailChain`、注入/PII/审核护栏 | [Guardrails](Guardrails.md) |
| `react_ai.observability` | 可观测性 | `AgentTracer`、`StructuredLogger`、`MetricsCollector` | [Observability](Observability.md) |
| `react_ai.prompts` | 提示词工程 | `PromptManager`、`ReActPrompt` | [Prompts](Prompts.md) |
| `react_ai.config` | 配置中心 | `AppSettings` 及各分组 Config | [Configuration](Configuration.md) |
| `api` | 服务接入 | FastAPI app + `/api/v1/agent/*` | [API Service](API-Service.md) |

## 一次请求的数据流

1. 客户端 `POST /api/v1/agent/chat/stream`，携带 `query` 与 `session_id`。
2. 路由层构造 `RunContext`（生成 `run_id` 作为 TraceID），调用 `ReActAgent.stream()`。
3. 引擎发出 `RUN_START` 事件 → 进入循环：
   - **Thought**：`llm.generate(messages, tools)`，`LLMRouter` 可在主模型失败时降级到备用模型；
   - **Action**：若返回 `tool_calls`，经 `ToolRegistry → ToolSandbox.run_safe()` 超时隔离执行；
   - **Observation**：工具结果以 `MessageRole.TOOL` 消息回写上下文，发出 `TOOL_END`/`OBSERVATION` 事件。
4. 模型不再请求工具时发出 `RUN_END`（含最终答复与全部 `StepResult`），事件以 SSE `data:` 帧逐条推送。
5. 全程生命周期钩子（`on_run_start/on_step_start/on_run_end/on_error`）可挂接审计日志与 Metrics。

## 实现状态说明

框架分层完整，但部分组件目前为**接口骨架/占位实现**（签名与契约已定型，逻辑待补全）：`SupervisorAgent.execute_task`、`StateGraphWorkflow.run`、`AgentRouter.route`、`RedisMessageStore` 读写方法、`ToolSandbox` 的 HITL 审批分支。二次开发时请以各模块页面标注为准。
