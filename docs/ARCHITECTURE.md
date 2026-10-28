# react-ai 企业级 Agent 架构设计与包规范文档

## 1. 架构愿景与设计理念

`react-ai` 是一套面向生产级环境打造的企业级 **AI Agent (智能体)** 基础架构。本框架基于 **ReAct (Reasoning + Acting)** 经典推理与行动协同范式，并结合了现代企业软件架构的 **高内聚低耦合**、**防腐层隔离**、**安全合规护栏** 与 **生产级可观测性** 要求。

### 核心架构原则：
1. **契约先行 (Contract-First)**: 数据协议、事件流、模型出入参、工具声明均采用强类型 Pydantic V2 严格约束；
2. **ReAct 闭环与熔断可控**: 驱动 `Thought (思考) -> Action (行动) -> Observation (观察)` 状态机，内置单次迭代超时、最大步数上限、Token 预算控制，杜绝死循环；
3. **多模型网关与容灾降级**: 抽象统一的 LLM Client，支持 OpenAI、Anthropic、DeepSeek 等主流大模型，支持自动故障转移 (Failover) 与重试；
4. **工具沙箱与人机协同 (HITL)**: 工具注册表与执行沙箱分离，具备高危指令拦截、权限校验与人工审批挂起 (Human-In-The-Loop) 机制；
5. **分层记忆治理**: 短期工作记忆 (滑动窗口/摘要压缩)、分布式会话缓存 (Redis TTL)、长期语义记忆 (向量知识库/Postgres) 分层存储；
6. **企业安全与可观测性**: 原生集成 Prompt 注入检测、敏感信息脱敏 (PII Masking)、OpenTelemetry 分布式链路追踪与结构化审计日志。

---

## 2. 整体架构与数据流拓扑图

```
                                +---------------------------------------------------+
                                |            Client Layer (Web / Chat / SDK)        |
                                +---------------------------------------------------+
                                                          | HTTP / SSE Stream
                                                          v
+-------------------------------------------------------------------------------------------------------------------+
|  API Gateway & Serving Layer (api/)                                                                               |
|  - FastAPI Endpoints: /api/v1/agent/chat, /api/v1/agent/chat/stream, /api/v1/agent/tools                         |
|  - Middlewares: CORS, Request Tracing (Trace-ID), Global Exception Handling                                       |
+-------------------------------------------------------------------------------------------------------------------+
                                                          |
                                                          v
+-------------------------------------------------------------------------------------------------------------------+
|  Core Runtime & State Engine (react_ai/core/)                                                                     |
|  - BaseAgent: 抽象智能体接口 (run / stream)                                                                        |
|  - ReActAgent: ReAct 执行状态机驱动核心                                                                            |
|  - RunContext: 全局运行上下文 (SessionID, TraceID, Token Budget, AbortSignal)                                      |
|  - AgentState & StepResult: 执行状态快照与历史步数推进                                                            |
|  - AgentLifecycleHook: 生命周期监听 (on_run_start, on_step_start, on_tool_call, on_error)                         |
+-------------------------------------------------------------------------------------------------------------------+
           |                          |                         |                        |
           v                          v                         v                        v
+-----------------------+  +----------------------+  +---------------------+  +---------------------+
| LLM Gateway           |  | Tool Ecosystem       |  | Hierarchical Memory |  | Security Guardrails |
| (react_ai/llm/)       |  | (react_ai/tools/)    |  | (react_ai/memory/)  |  | (guardrails/)       |
| - LLMRouter: 降级容灾 |  | - ToolRegistry       |  | - WindowBuffer      |  | - Prompt Injection  |
| - OpenAI Provider     |  | - ToolSandbox: 超时  |  | - Redis Store (TTL) |  | - PII Masking       |
| - DeepSeek Provider   |  | - Builtin: Bash,     |  | - LongTerm Semantic |  | - Compliance &      |
| - Anthropic Provider  |  |   HTTP, Knowledge RAG|  |   Vector Memory     |  |   Moderation        |
+-----------------------+  +----------------------+  +---------------------+  +---------------------+
           ^                          ^                         ^                        ^
           +--------------------------+-------------------------+------------------------+
                                                  |
                                                  v
+-------------------------------------------------------------------------------------------------------------------+
|  Enterprise Cross-Cutting Concerns                                                                                |
|  - Observability (react_ai/observability/): OpenTelemetry Tracer, Structured JSON Logger, Metrics Collector      |
|  - Prompt Management (react_ai/prompts/): ReAct System Prompt, Jinja2 Template Engine, Versioning                 |
|  - Configuration (react_ai/config/): Pydantic Settings, Env Overrides, Secret Governance                          |
|  - Schemas (react_ai/schemas/): ChatMessage, AgentEvent, ToolDefinition, StepResult                               |
+-------------------------------------------------------------------------------------------------------------------+
```

---

## 3. 项目目录树结构

```text
react-ai/
├── src/
│   └── react_ai/                      # 框架主包 (可作为独立 wheel 库发布)
│       ├── __init__.py                # 框架顶层导出
│       │
│       ├── core/                      # 【核心层】Agent 运行时与推理引擎
│       │   ├── __init__.py
│       │   ├── base.py                # Agent 抽象基类 BaseAgent (run/stream)
│       │   ├── engine.py              # ReAct 核心执行引擎 (Thought -> Action -> Observation)
│       │   ├── context.py             # 运行上下文 RunContext (TraceID, Budget, Cancellation)
│       │   ├── state.py               # 智能体状态快照 AgentState
│       │   └── lifecycle.py           # 生命周期钩子 AgentLifecycleHook
│       │
│       ├── schemas/                   # 【契约层】数据模型与通用协议定义 (Pydantic V2)
│       │   ├── __init__.py
│       │   ├── messages.py            # 统一消息模型 (ChatMessage, ToolCall, UsageStats)
│       │   ├── events.py              # 流式运行事件模型 (AgentEvent, AgentEventType)
│       │   ├── agent.py               # 智能体配置与响应契约 (AgentConfig, AgentResponse, StepResult)
│       │   └── tools.py               # 工具声明与执行结果模型 (ToolDefinition, ToolParameterSchema)
│       │
│       ├── llm/                       # 【模型网关层】统一模型抽象与多提供商网关
│       │   ├── __init__.py
│       │   ├── base.py                # BaseLLMClient 抽象接口定义
│       │   ├── client.py              # 模型客户端工厂 (create_llm_client)
│       │   ├── router.py              # 智能路由网关 (负载均衡、异常降级容灾 Fallback)
│       │   └── providers/             # 适配器实现
│       │       ├── __init__.py
│       │       ├── openai.py          # OpenAI (GPT-4o 等) 适配器
│       │       ├── deepseek.py        # DeepSeek (V3/R1) 适配器
│       │       └── anthropic.py       # Anthropic (Claude 3.5 Sonnet) 适配器
│       │
│       ├── tools/                     # 【工具层】工具沙箱、注册中心与内置插件
│       │   ├── __init__.py
│       │   ├── base.py                # BaseTool 抽象类与 @tool 装饰器
│       │   ├── registry.py            # 工具注册表 ToolRegistry (发现、鉴权、管理)
│       │   ├── sandbox.py             # 工具隔离沙箱 ToolSandbox (超时中断、人机协同审批)
│       │   └── builtin/               # 内置企业工具集
│       │       ├── __init__.py
│       │       ├── bash.py            # 受控系统指令工具 SafeBashTool (内置黑名单)
│       │       ├── http.py            # RESTful API 调用工具 HttpTool
│       │       └── retrieval.py       # 企业知识库向量语义检索工具 KnowledgeRetrievalTool
│       │
│       ├── memory/                    # 【记忆层】分层持久化与上下文管理
│       │   ├── __init__.py
│       │   ├── base.py                # BaseMemory 与 BaseMessageStore 抽象接口
│       │   ├── short_term.py          # 短期工作记忆 (WindowBufferMemory 滑动窗口)
│       │   ├── long_term.py           # 长期语义记忆 (LongTermSemanticMemory 向量关联)
│       │   └── stores/                # 底层存储驱动适配器
│       │       ├── __init__.py
│       │       ├── in_memory.py       # 本地内存存储 (用于测试与本地调试)
│       │       ├── redis.py           # Redis 分布式会话缓存 (支持 TTL 自动过期)
│       │       └── postgres.py        # PostgreSQL 持久化会话归档
│       │
│       ├── orchestration/             # 【编排层】多智能体协作与工作流
│       │   ├── __init__.py
│       │   ├── router.py              # 意图路由分发器 AgentRouter
│       │   ├── supervisor.py          # 主管-工作节点分层协作 SupervisorAgent
│       │   └── workflow.py            # 图驱动与 DAG 状态机编排 StateGraphWorkflow
│       │
│       ├── guardrails/                # 【安全护栏层】企业合规与安全防御
│       │   ├── __init__.py
│       │   ├── base.py                # BaseGuardrail 安全校验基类
│       │   ├── injection.py           # Prompt 注入攻击与越狱拦截器
│       │   ├── pii.py                 # 个人隐私数据脱敏掩码 (手机号、身份证等)
│       │   └── moderation.py          # 敏感词过滤与企业合规审核
│       │
│       ├── observability/             # 【可观测性层】链路追踪与系统度量
│       │   ├── __init__.py
│       │   ├── tracer.py              # OpenTelemetry 分布式链路追踪器
│       │   ├── logger.py              # 结构化 JSON 审计日志器 (关联 TraceID)
│       │   └── metrics.py             # 性能与开销度量器 (Token 消耗、延时、错误率)
│       │
│       ├── prompts/                   # 【提示词工程层】提示词生命周期与模板管理
│       │   ├── __init__.py
│       │   ├── base.py                # Prompt 基类、消息协议 (PromptMessage)、渲染契约与审计
│       │   ├── manager.py             # 模板注册与 Jinja2 / BasePrompt 渲染管理器
│       │   └── templates/             # 预置模板目录
│       │       ├── __init__.py
│       │       └── react.py           # 企业标准 ReAct 思考提示词
│       │
│       └── utils/                     # 【工具工具层】通用辅助组件
│           ├── __init__.py
│           ├── serializer.py          # 安全 JSON 序列化器
│           └── token_counter.py       # Token 估算与预算检查工具
│
├── api/                               # 【服务端层】FastAPI Web / SSE 服务
│   ├── __init__.py
│   ├── main.py                        # FastAPI 应用入口与中间件装配
│   └── routes/                        # 路由控制器目录
│       ├── __init__.py
│       └── agent.py                   # Agent 对话、流式与工具查询端点
│
├── tests/                             # 【测试套件】分层自动化测试
│   ├── __init__.py
│   ├── unit/                          # 单元测试 (核心引擎、工具注册、护栏逻辑)
│   │   ├── __init__.py
│   │   └── test_engine.py
│   ├── integration/                   # 集成测试 (存储层与网关连通性)
│   └── e2e/                           # 端到端测试 (完整链路场景测试)
│
├── examples/                          # 【示例代码】开发者接入范例
│   └── quickstart.py                  # 最小可运行 Demo (快速上手)
│
├── docs/                              # 【工程文档】架构与使用文档
│   └── ARCHITECTURE.md                # 架构设计与包说明文档 (本文档)
│
├── pyproject.toml                     # 项目现代打包与依赖规范 (PEP 517/621)
└── README.md                          # 项目自述文档
```

---

## 4. 各核心包功能与设计详述

### 4.1 `react_ai.core` —— Agent 运行时核心引擎
- **职责定位**: 智能体执行的心脏。控制 ReAct 推理状态机的循环推进、步骤决策与生命周期管理。
- **关键模块**:
  - `base.py`: 定义 `BaseAgent` 统一抽象，声明 `run(query, context) -> AgentResponse` (批处理同步调用) 与 `stream(query, context) -> AsyncGenerator[AgentEvent, None]` (全链路流式事件响应)。
  - `engine.py`: `ReActAgent` 实现。严格执行 `Thought` 阶段 -> 模型调用 -> 工具决策 (判断是否有 `tool_calls`) -> `Action` 阶段 -> 调用工具执行沙箱 -> `Observation` 阶段 (将结果注入对话上下文) -> 循环至产生最终结论。
  - `context.py`: `RunContext` 携带分布式 `session_id`、`run_id` (TraceID)、`budget_tokens` (单次会话 Token 熔断上限) 以及 `is_cancelled` (主动取消信号)。
  - `state.py`: 保存不可变或审计用的执行步骤快照 `AgentState` 与 `StepResult`。
  - `lifecycle.py`: 提供 `AgentLifecycleHook` 钩子，支持外部注入监控、审计日志和外部系统联动。

### 4.2 `react_ai.schemas` —— 强类型数据契约层
- **职责定位**: 消除弱类型字典传递导致的隐式缺陷，规范系统内部流转数据格式。
- **关键模块**:
  - `messages.py`: 适配标准 OpenAI 格式的 `ChatMessage`、`ToolCall`、`ToolCallResult`，以及详细记录 Token 消耗的 `UsageStats`。
  - `events.py`: 细粒度流式事件 `AgentEvent`。包含 `RUN_START`、`THOUGHT_START`、`TOOL_START`、`TOOL_END`、`OBSERVATION`、`RUN_END` 等 10+ 种状态，使前端可以实时渲染 Agent 的打字机思考、工具调用卡片与执行进度。
  - `agent.py`: 包含 `AgentConfig` (温度、最大步数、超时) 与 `AgentResponse`。
  - `tools.py`: 包含声明工具 JSON Schema 的 `ToolDefinition`。

### 4.3 `react_ai.llm` —— 多模型统一抽象与路由网关
- **职责定位**: 解耦具体大模型厂商 API，屏蔽各厂商协议差异，提供企业级容灾降级机制。
- **关键模块**:
  - `base.py`: `BaseLLMClient` 统一出入参规范，抹平各平台结构差异。
  - `router.py`: `LLMRouter` 支持主备降级链 (Primary -> Fallbacks)。例如主用 DeepSeek V3 发生限流或网络抖动时，透明且无缝地自动重试并降级至 OpenAI 或 Azure，保障企业 99.9% 可用性。
  - `providers/`: 模块化适配器。
    - `openai.py`: 支持标准兼容协议与兼容接口网关。
    - `deepseek.py`: 支持 DeepSeek 深度推理系列。
    - `anthropic.py`: 支持 Claude 系列。
  - `client.py`: 提供工厂函数 `create_llm_client()`，支持基于配置一键实例化。

### 4.4 `react_ai.tools` —— 工具沙箱、权限与内置插件
- **职责定位**: Agent 与现实物理系统交互的执行边界，负责安全隔离、参数校验、调用审计与人工批准。
- **关键模块**:
  - `base.py`: 提供 `BaseTool` 接口，同时提供声明式 `@tool` 装饰器，使任意 Python 业务函数均可秒级转变为 Agent 工具。
  - `registry.py`: `ToolRegistry` 提供工具动态注册、元数据提取、按名称检索与安全分发。
  - `sandbox.py`: `ToolSandbox`。工具执行时的“隔离保护舱”，执行异步超时熔断 (`asyncio.wait_for`)，并支持 `requires_approval=True` 的人机协同 (Human-In-The-Loop) 审批挂起。
  - `builtin/`:
    - `bash.py`: `SafeBashTool`，内置 `rm -rf`、`format` 等高危危险指令黑名单阻断。
    - `http.py`: `HttpTool`，提供企业标准化 RESTful 外部系统调优能力。
    - `retrieval.py`: `KnowledgeRetrievalTool`，支持企业私有知识库召回。

### 4.5 `react_ai.memory` —— 层次化多级记忆系统
- **职责定位**: 支撑长轮次复杂会话上下文管理，兼顾上下文窗口容量、响应延时与持久化需求。
- **关键模块**:
  - `base.py`: `BaseMemory` 与 `BaseMessageStore` 抽象类。
  - `short_term.py`: `WindowBufferMemory`，短期工作记忆，通过滑动窗口算法仅保留最近 K 轮核心交互，降低 Token 膨胀。
  - `long_term.py`: `LongTermSemanticMemory`，根据用户当前输入，跨会话检索向量知识库或历史用户偏好画像并按需召回。
  - `stores/`:
    - `in_memory.py`: 单机易失存储，适用于单元测试与本地演示。
    - `redis.py`: 分布式会话缓存，具备 TTL 自动失效，适合多实例横向扩展。
    - `postgres.py`: 关系型持久化存储，负责全量审计日志与历史存档。

### 4.6 `react_ai.orchestration` —— 多智能体协作与工作流编排
- **职责定位**: 支持从单 Agent 向 Multi-Agent 多智能体团队演进。
- **关键模块**:
  - `base.py`: `BaseOrchestrator` 企业级多智能体编排器基类，提供统一 Agent 注册管理、多协议调用适配 (`execute`/`run`/`callable`)、同步/异步双模执行引擎、超时控制与重试机制 (`max_retries`)、快速失败 (`fail_fast`)、编排及 Agent 级全生命周期钩子 (`pre_hooks`/`post_hooks`/`agent_hooks`) 与历史执行指标统计 (`get_stats`/`get_metrics`)；内置派生开箱即用的顺序串行编排器 `SequentialOrchestrator` 与并发编排器 `ParallelOrchestrator`。
  - `flow.py`: 流程编排重定向导出，对齐 `BaseFlow` 业务管道编排契约。
  - `router.py`: `AgentRouter`，根据用户输入的意图分类，动态分发至专精领域的子智能体 (例如：代码智能体、客服智能体、财务审计智能体)。
  - `supervisor.py`: `SupervisorAgent`，主管-工人 (Supervisor-Worker) 模式，主管负责任务拆解、分派并汇总下级 Worker 的产出。
  - `workflow.py`: `StateGraphWorkflow`，状态图驱动编排，支持有向无环图 (DAG) 与条件分支流转。

### 4.7 `react_ai.guardrails` —— 企业级安全合规护栏
- **职责定位**: 针对大模型落地中的安全、隐私合规与内容风险，构筑坚固防线。
- **关键模块**:
  - `base.py`: `BaseGuardrail` 统一安全护栏基类，提供统一输入/输出上下文 (`GuardrailContext`)、规则动态注册、执行前/后生命周期钩子、熔断器 (`Circuit Breaker`)、度量统计 (`get_stats`) 与安全审计；提供基于正则的通用关键词护栏 `KeywordGuardrail` 以及串行组合执行链 `GuardrailChain`，向后兼容异步协程协议。
  - `injection.py`: `PromptInjectionGuardrail`，正则与模式匹配识别“忽略以上指示”、“system prompt override”等 Prompt 注入与越狱攻击。
  - `pii.py`: `PIIMaskingGuardrail`，输入前与输出后自动对大陆手机号、身份证号等敏感隐私数据进行脱敏遮罩 (Masking)。
  - `moderation.py`: `ModerationGuardrail`，违禁词与企业自定义违规审核。

### 4.8 `react_ai.observability` —— 生产可观测性体系
- **职责定位**: 解决 LLM 应用“黑盒”不可见难题，提供全链路调试、监控与计费审计。
- **关键模块**:
  - `tracer.py`: 原生对接 OpenTelemetry 分布式追踪，记录 Agent 思考、工具调用与模型交互的 Span 树。
  - `logger.py`: 结构化 JSON 日志器，自动注入会话 ID、Trace-ID，便于接入 ELK、Loki 等企业日志收集系统。
  - `metrics.py`: 收集调用总频次、请求延时分布、Token 总消耗量、工具执行故障率等关键性能指标。

### 4.9 `react_ai.prompts` —— 提示词工程管理
- **职责定位**: 提示词的集中化管理与动态注入，消除散落在业务代码中的硬编码字符串。
- **关键模块**:
  - `templates/react.py`: 经过验证的企业级标准 ReAct 系统提示词。
  - `manager.py`: 基于 Jinja2 的模板引擎，支持在运行时根据上下文变量渲染 Prompt，并支持模板热更新。

### 4.10 `react_ai.utils` —— 基础工具组件
- **职责定位**: 框架内部共用的非业务通用函数。
- **关键模块**:
  - `serializer.py`: 安全处理包含复杂对象、Pydantic 模型的 JSON 序列化。
  - `token_counter.py`: 快速 Token 估算器，防止发送超长上下文导致模型报错。

### 4.11 `api` —— 服务接入层 (FastAPI)
- **职责定位**: 对外暴露标准的微服务网络接口。
- **关键端点**:
  - `POST /api/v1/agent/chat`: 一次性同步阻塞对话返回；
  - `POST /api/v1/agent/chat/stream`: Server-Sent Events (SSE) 协议的高性能流式响应，支持前端打字机展示思考流程；
  - `GET /api/v1/agent/tools`: 查询当前注册的可用工具清单；
  - `GET /health`: 容器编排 (K8s) 探针健康检查。

---

## 5. 企业级落地特性与工程最佳实践

1. **环境配置安全**: 核心配置统一由 `.env` 或操作系统环境变量注入，敏感密钥杜绝代码硬编码。
2. **测试与质量保证**:
   - `tests/unit/`: 借助 Mock Client 实现纯单机、零网络依赖的 ReAct 状态机闭环单测；
   - 依赖标准化打包规范 (`pyproject.toml`)，兼容 `pip install -e .` 或 Hatchling 构建。
3. **扩展指南**:
   - **新增自定义工具**: 使用 `@tool` 装饰器定义函数，并调用 `registry.register(func)`；
   - **新增模型厂商**: 继承 `BaseLLMClient` 实现 `generate` 与 `stream_generate`；
   - **新增存储后端**: 继承 `BaseMessageStore` 实现存储读写接口。
