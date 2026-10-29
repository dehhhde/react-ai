# react-ai Wiki

> **Enterprise-grade Production AI Agent Framework (Reasoning + Acting)**
> 面向企业级生产环境打造的高可靠、可观测、强管控 Python AI 智能体开发框架。

`react-ai` 基于经典 **ReAct (Thought → Action → Observation)** 推理范式，提供多模型网关容灾、工具沙箱与 HITL 审批、分层记忆、安全合规护栏、OpenTelemetry 可观测性以及 FastAPI/SSE 微服务接入的一站式 Agent 运行时。

- 语言 / 运行时：Python >= 3.10，全异步 (asyncio)
- 数据契约：Pydantic V2 强类型
- 版本：`0.1.0`（见 [pyproject.toml](file:///d:/code/react-ai/pyproject.toml)）
- 许可证：Apache-2.0

---

## Wiki 目录

### 入门

| 页面 | 内容 |
|---|---|
| [Quickstart](Quickstart.md) | 安装、环境配置、第一个 ReAct Agent、启动 API 服务 |
| [Configuration](Configuration.md) | 全部环境变量与 `AppSettings` 配置参考 |
| [Architecture](Architecture.md) | 分层架构、数据流拓扑与包职责总览 |

### 核心模块

| 页面 | 模块 | 内容 |
|---|---|---|
| [Core Engine](Core-Engine.md) | `react_ai.core` | ReActAgent 状态机、RunContext、AgentState、生命周期钩子、事件协议 |
| [LLM Gateway](LLM-Gateway.md) | `react_ai.llm` | BaseLLMClient 抽象、多厂商 Provider、工厂函数、LLMRouter 容灾降级 |
| [Tools](Tools.md) | `react_ai.tools` | `@tool` 装饰器、ToolRegistry、ToolSandbox 沙箱、内置工具、售后业务工具集 |
| [Memory](Memory.md) | `react_ai.memory` | BaseMemory/MemoryItem 模型、滑动窗口短期记忆、长期语义记忆、Redis/Postgres 存储 |
| [Orchestration](Orchestration.md) | `react_ai.orchestration` / `react_ai.flows` | 多智能体编排器、Supervisor、意图路由、状态图工作流、BaseFlow 流程管道 |
| [Guardrails](Guardrails.md) | `react_ai.guardrails` | Prompt 注入拦截、PII 脱敏、合规审核、护栏组合链 |
| [Intent-Recognition](Intent-Recognition.md) | `examples/aftersales_langgraph.py` | 意图识别与槽位填充现状评估、企业级差距分析与改造路线（补充文档） |
| [Intent-Recognition-Optimization](Intent-Recognition-Optimization.md) | `examples/aftersales_langgraph.py` | 第二轮优化方案：评测集/可观测性/置信度/换意图/输出护栏（规划文档） |

### 支撑服务与接入

| 页面 | 模块 | 内容 |
|---|---|---|
| [Observability](Observability.md) | `react_ai.observability` | OpenTelemetry 追踪、结构化 JSON 日志、Metrics 度量 |
| [Prompts](Prompts.md) | `react_ai.prompts` | PromptManager、Jinja2 模板、企业标准 ReAct 提示词 |
| [API Service](API-Service.md) | `api/` | FastAPI REST 端点与 SSE 流式协议 |
| [Development Guide](Development-Guide.md) | `tests/` `examples/` | 测试套件、示例脚本、扩展开发指南 |

---

## 核心特性速览

- **ReAct 核心推理引擎**：完整状态机推进，内置最大步数熔断（默认 `max_steps=15`）、协作式取消与 Token 预算控制。
- **多模型网关与容灾降级**：统一抽象 OpenAI / DeepSeek / Anthropic，`LLMRouter` 支持主备链路无缝 Failover。
- **安全工具沙箱与 HITL**：声明式 `@tool` 注册，调用超时隔离（默认 30s），高危工具人工审批挂起。
- **分层记忆治理**：短期滑动窗口、Redis 分布式会话缓存 (TTL)、PostgreSQL 历史审计归档。
- **企业安全护栏**：Prompt 注入阻断、PII 自动脱敏、敏感词合规审核，支持 `GuardrailChain` 串行组合。
- **生产级可观测性**：OpenTelemetry Trace/Span、结构化 JSON 日志、Token/延时/错误率度量。
- **FastAPI 微服务集成**：REST + SSE 打字机流式端点开箱即用。

## 相关文档

- 项目自述：[README.md](../README.md)
- 架构设计原文：[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)
