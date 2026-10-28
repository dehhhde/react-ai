# react-ai

> **Enterprise-grade Production AI Agent Framework (Reasoning + Acting)**
> 面向企业级生产环境打造的高可靠、可观测、强管控 Python AI 智能体开发框架。

---

## 🌟 核心特性

- **ReAct 核心推理引擎**: 完整的 `Thought -> Action -> Observation` 状态机推进，支持最大步数上限、超时熔断与 Token 预算控制。
- **多模型网关与容灾降级**: 统一抽象 OpenAI、DeepSeek、Anthropic 等模型接口，支持主备链路无缝故障切换 (Failover) 与指数退避重试。
- **安全工具沙箱与 HITL**: 提供声明式 `@tool` 快速注册，内置指令安全拦截、调用超时隔离与人机协同 (Human-In-The-Loop) 审批挂起。
- **分层记忆治理**: 支持短时滑动窗口上下文缓冲、Redis 分布式缓存 (TTL) 以及 PostgreSQL 历史审计归档。
- **企业安全护栏**: 原生内置 Prompt 注入阻断、个人隐私信息 (PII) 自动脱敏遮罩、合规敏感词审查。
- **生产级可观测性**: 原生集成 OpenTelemetry 链路追踪 (Trace/Span)、结构化 JSON 日志审计与性能开销度量。
- **FastAPI 微服务集成**: 开箱即用的 RESTful 端点与 Server-Sent Events (SSE) 高性能打字机流式输出。

---

## 📁 目录架构概览

```text
react-ai/
├── src/react_ai/              # 核心包源代码
│   ├── core/                  # Agent 运行时状态机 (ReActAgent, RunContext, State, Lifecycle)
│   ├── schemas/               # 强类型数据模型与事件协议 (Pydantic V2)
│   ├── llm/                   # 统一大模型抽象、多模型路由网关与厂商适配器
│   ├── tools/                 # 工具基类、注册表、安全隔离沙箱与内置插件
│   ├── memory/                # 分层持久化记忆系统 (滑动窗口、Redis、Postgres)
│   ├── orchestration/         # 多智能体协同编排 (Supervisor-Worker, Router, Workflow)
│   ├── guardrails/            # 安全合规护栏 (Prompt注入拦截、PII脱敏、风控审核)
│   ├── observability/         # 可观测性体系 (OpenTelemetry追踪、结构化日志、Metrics)
│   ├── prompts/               # 提示词工程管理与 Jinja2 模板引擎
│   ├── config/                # 集中配置中心 (Pydantic-Settings, 环境变量分层)
│   └── utils/                 # 通用序列化与 Token 预算辅助工具
├── api/                       # FastAPI 微服务层 (REST & SSE 流式端点)
├── tests/                     # 自动化测试套件 (单元测试、集成测试、端到端测试)
├── examples/                  # 快速入门与实战演示脚本
├── docs/                      # 架构设计与各包详尽规范文档 (docs/ARCHITECTURE.md)
└── pyproject.toml             # 项目构建与依赖声明
```

> 详细的各模块包职责、内部数据流与扩展开发规范，请参阅 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 🚀 快速上手 (Quickstart)

### 1. 安装依赖

```bash
pip install -e .
```

### 2. 编写你的第一个 ReAct Agent

```python
import asyncio
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.tools.base import tool
from react_ai.tools.registry import ToolRegistry
from react_ai.llm.client import create_llm_client

# 定义工具
@tool(name="calc", description="计算数学表达式")
def calc(expr: str) -> str:
    return str(eval(expr, {"__builtins__": {}}))

async def main():
    # 注册工具
    registry = ToolRegistry()
    registry.register(calc)

    # 实例化 LLM 客户端与 ReAct Agent
    llm = create_llm_client("openai")
    agent = ReActAgent(llm=llm, tool_registry=registry)

    # 发起流式执行
    context = RunContext()
    async for event in agent.stream("请计算 (25 * 4) + 50 的结果", context):
        print(f"[{event.event_type.value}] -> {event.payload}")

if __name__ == "__main__":
    asyncio.run(main())
```

### 3. 启动 API 服务

```bash
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

访问 `http://localhost:8000/docs` 查看 Swagger 交互式接口文档。

---

## 🧪 运行测试

```bash
python -c "
import sys; sys.path.insert(0, 'src')
import unittest
loader = unittest.TestLoader()
suite = loader.discover('tests')
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)
assert result.wasSuccessful()
"
```

---

## 📄 开源许可证

本项目遵循 Apache-2.0 许可证。
