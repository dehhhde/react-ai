# Quickstart 快速上手

## 1. 环境要求

- Python >= 3.10
- 可选依赖服务：Redis（分布式会话缓存）、PostgreSQL（历史归档 / 售后业务库）

## 2. 安装

```bash
# 基础安装（可编辑模式）
pip install -e .

# 开发依赖（pytest / mypy / ruff）
pip install -e ".[dev]"

# 按需安装存储后端
pip install -e ".[redis]"
pip install -e ".[postgres]"
```

## 3. 配置环境变量

复制 `.env.example` 为 `.env` 并填入真实密钥（详见 [Configuration](Configuration.md)）：

```bash
LLM_DEFAULT_PROVIDER=deepseek
LLM_MODEL_NAME=deepseek-chat
LLM_API_KEY=sk-your-api-key
LLM_API_BASE=https://api.deepseek.com
```

所有配置通过 `react_ai.config.settings.settings`（`AppSettings` 单例）自动读取，支持 `.env` 文件与操作系统环境变量分层覆盖。

## 4. 编写第一个 ReAct Agent

参考 [examples/quickstart.py](../examples/quickstart.py)：

```python
import asyncio
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.tools.base import tool
from react_ai.tools.registry import ToolRegistry
from react_ai.llm.client import create_llm_client
from react_ai.schemas.events import AgentEventType

# 1) 声明式定义工具
@tool(name="calculate", description="计算数学算术表达式结果，入参 expression 为字符串表达式")
def calculate(expression: str) -> str:
    return str(eval(expression, {"__builtins__": {}}))

async def main():
    # 2) 注册工具
    registry = ToolRegistry()
    registry.register(calculate)

    # 3) 创建 LLM 客户端（默认按 .env 中的 provider 实例化）与 Agent
    llm = create_llm_client()
    agent = ReActAgent(llm=llm, tool_registry=registry)

    # 4) 流式执行并监听全生命周期事件
    ctx = RunContext()
    async for event in agent.stream("请帮我计算 (35 * 12) + 180 等于多少？", ctx):
        if event.event_type == AgentEventType.TOOL_START:
            print(f"调用工具: {event.payload['tool']} args={event.payload['args']}")
        elif event.event_type == AgentEventType.RUN_END:
            print(f"最终答复: {event.payload['output']}")

asyncio.run(main())
```

运行：

```bash
python examples/quickstart.py
```

### 同步调用

不需要流式事件时，使用 `run()` 获得聚合的 `AgentResponse`：

```python
response = await agent.run("你好，介绍一下你自己", RunContext())
print(response.final_output)   # 最终答复文本
print(response.steps)          # 每步 StepResult（thought/action/observation）
print(response.success)        # 是否正常结束
```

## 5. 启动 API 服务

```bash
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

- Swagger 交互文档：`http://localhost:8000/docs`
- 健康检查：`GET /health`
- 同步对话：

```bash
curl -X POST http://localhost:8000/api/v1/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "查询订单 ORD-1001 的物流", "session_id": "demo"}'
```

- SSE 流式对话（逐条推送 `AgentEvent` JSON）：

```bash
curl -N -X POST http://localhost:8000/api/v1/agent/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "帮我算 (25*4)+50", "session_id": "demo"}'
```

端点完整说明见 [API Service](API-Service.md)。

## 6. 下一步

- 理解执行循环 → [Core Engine](Core-Engine.md)
- 接入多模型容灾 → [LLM Gateway](LLM-Gateway.md)
- 注册业务工具与审批 → [Tools](Tools.md)
- 多智能体协作 → [Orchestration](Orchestration.md)
