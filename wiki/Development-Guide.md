# Development Guide 开发指南

## 开发环境

```bash
pip install -e ".[dev]"        # pytest / pytest-asyncio / mypy / ruff
cp .env.example .env           # 填入真实 LLM_API_KEY 等
```

## 运行测试

测试套件位于 `tests/`，分三层：`unit/`（单元测试）、`integration/`（集成）、`e2e/`（端到端）。

```bash
# 标准 pytest
pytest tests/unit -v

# 零依赖方式（无需安装，直接 unittest 发现）
python -c "
import sys; sys.path.insert(0, 'src')
import unittest
suite = unittest.TestLoader().discover('tests')
result = unittest.TextTestRunner(verbosity=2).run(suite)
assert result.wasSuccessful()
"
```

单元测试通过 **Mock LLM 客户端**（实现 `BaseLLMClient.generate` 返回预置 `LLMResponse`/`tool_calls`）实现纯单机、零网络依赖的 ReAct 状态机闭环验证。现有用例覆盖：引擎、工具、护栏、记忆、编排、流程、事件、Agent、提示词、售后工具、base_model_client 等（见 [tests/unit/](file:///d:/code/react-ai/tests/unit)）。

## 示例脚本

| 脚本 | 演示内容 |
|---|---|
| [examples/quickstart.py](../examples/quickstart.py) | DeepSeek 驱动的最小 ReAct Agent + 全生命周期事件打印 |
| [examples/flow_demo.py](../examples/flow_demo.py) | `BaseFlow` 订单履约流程：条件步骤、重试、同步/异步混合 |
| [examples/aftersales_langgraph.py](../examples/aftersales_langgraph.py) | LangGraph 集成 + 售后业务 Agent 全链路 |

均可直接 `python examples/xxx.py` 运行（脚本自动把 `src/` 注入 `sys.path`）。

## scripts/ 维护脚本

`scripts/` 为一次性运维/脚手架脚本：`init_aftersales_db.py`（初始化售后 PostgreSQL 库）、`build_memory.py`、`update_settings.py` 等历史补丁脚本。新环境搭建仅需 `init_aftersales_db.py`。

## 扩展开发规范

### 新增自定义工具

```python
from react_ai.tools.base import tool

@tool(name="send_email", description="发送邮件", side_effects="write",
      requires_approval=True, timeout_seconds=10.0)
async def send_email(to: str, subject: str, body: str) -> str:
    ...
registry.register(send_email)
```

### 新增模型厂商

继承 `BaseLLMClient` 实现 `generate` / `stream_generate`（见 [LLM Gateway](LLM-Gateway.md)），在 `create_llm_client` 增加分支或直接手工注入。

### 新增存储后端

继承 `BaseMessageStore` 实现 `get_messages` / `add_message` / `clear`；如需 `MemoryItem` 级能力则继承 `BaseMemory` 实现 `_do_write/_do_read/_do_delete/_evict`（见 [Memory](Memory.md)）。

### 新增护栏

继承 `BaseGuardrail` 覆写 `_check_impl`，用 `register_rule` 声明规则，加入 `GuardrailChain`（见 [Guardrails](Guardrails.md)）。

## 代码风格与质量

- Ruff（lint + format）与 mypy 已在 dev 依赖中：`ruff check src tests`、`mypy src`
- 全部对外 API 保持类型注解；数据出入使用 Pydantic 模型而非裸 dict
- 异步优先：I/O 型工具/Provider 实现 `async def`，同步函数由沙箱自动 `to_thread` 包装

## 打包发布

构建系统为 Hatchling（PEP 517/621），wheel 仅打包 `src/react_ai`：

```bash
pip install build
python -m build          # 产出 dist/react_ai-0.1.0-py3-none-any.whl
```
