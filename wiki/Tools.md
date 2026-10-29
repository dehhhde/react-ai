# Tools 工具生态

模块：`react_ai.tools`。工具是 Agent 与外部系统交互的执行边界，框架提供声明式注册、集中注册表与隔离沙箱三层能力。

## @tool 声明式装饰器

文件：[tools/base.py](file:///d:/code/react-ai/src/react_ai/tools/base.py)

```python
@tool(
    name: str | None = None,               # 缺省取函数名
    description: str | None = None,        # 缺省取 docstring
    side_effects: str = "none",            # none / read / write / destructive
    tags: list[str] | None = None,
    requires_approval: bool = False,       # HITL 人工审批
    timeout_seconds: float = 30.0,
    input_schema: dict | None = None,      # 缺省由函数签名+类型注解自动推导 JSON Schema
)
```

装饰器将普通函数包装为 `FunctionTool`（`BaseTool` 子类）。参数类型注解自动映射为 JSON Schema 类型，无默认值的参数进入 `required`。

```python
@tool(name="query_user", description="按用户ID查询档案", tags=["crm"])
async def query_user(user_id: str, verbose: bool = False) -> dict:
    ...
```

`BaseTool` 关键属性默认值：`side_effects="none"`、`tags=[]`、`requires_approval=False`、`timeout_seconds=30.0`；`get_definition()` 生成供 LLM Function Calling 使用的 `ToolDefinition` 元数据。

`FunctionTool.execute()` 会自动把名为 `ctx` / `tool_context` / `context` 的参数注入为 `ToolContext`，其余按名称从 `arguments` 匹配。

## ToolContext 与 ToolResult

- `ToolContext`：`caller_id`、`tenant_id`、`user_id`、`trace_id`、`idempotency_key` —— 沙箱从 `RunContext` 自动派生（见下）。
- `ToolResult`：`ok`、`data`、`error_type`（`auth` / `business` / 其他）、`error_code`、`error_message`。

## ToolRegistry 注册表

文件：[tools/registry.py](file:///d:/code/react-ai/src/react_ai/tools/registry.py)

```python
class ToolRegistry:
    def __init__(self, sandbox: ToolSandbox | None = None): ...
    def register(self, tool: BaseTool) -> None
    def get(self, name: str) -> BaseTool | None
    def list_definitions(self) -> list[ToolDefinition]      # 提供给 LLM 的工具清单
    async def execute(self, name, arguments, context: RunContext | None) -> Any   # 经沙箱执行
    async def call(self, name, arguments, context) -> ToolResult  # 直接调用并返回结构化结果
```

`execute()` 找不到工具抛 `KeyError`；参数校验（必填、未知字段、类型、枚举、数值范围）在 `BaseTool` 层完成并返回结构化错误。

## ToolSandbox 隔离沙箱

文件：[tools/sandbox.py](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py)

```python
class ToolSandbox:
    def __init__(self, default_timeout: float = 30.0): ...
    async def run_safe(self, tool: BaseTool, arguments: dict,
                       context: RunContext | None = None) -> Any
```

`run_safe()` 行为：

1. **HITL 检查点**：`requires_approval=True` 或 `side_effects == "destructive"` 的工具触发人工审批中断（当前为预留检查分支）。
2. **超时熔断**：取 `tool.timeout_seconds`，缺省用沙箱 `default_timeout`；同步 `call` 走 `asyncio.to_thread` 避免阻塞事件循环，超时抛 `TimeoutError`。
3. **上下文派生**：`RunContext.metadata` 中的 `caller_id` / `tenant_id` / `idempotency_key` 与 `user_id`、`run_id` 自动映射为 `ToolContext`。
4. **错误类型转换**：`ToolResult.error_type` 为 `auth` → `PermissionError`；`business` → `ValueError`；其他 → `RuntimeError("[code] message")`。

## 内置工具

目录：[tools/builtin/](file:///d:/code/react-ai/src/react_ai/tools/builtin)

| 工具 | 说明 |
|---|---|
| `SafeBashTool` (`bash`) | 受限系统指令执行。`side_effects="destructive"`、`requires_approval=True`，内置黑名单 `rm -rf` / `format` / `del /f` / `mkfs`，命中即抛 `PermissionError`；`subprocess` 硬超时 15s |
| `HttpTool` (`http`) | 标准化 RESTful 外部 API 调用 |
| `KnowledgeRetrievalTool` | 企业知识库语义检索召回 |

## 售后业务工具集（示例领域包）

文件：[tools/aftersales.py](file:///d:/code/react-ai/src/react_ai/tools/aftersales.py)。基于 `AfterSalesBaseTool` 基类，对接 PostgreSQL 电商售后库（`settings.database`），是 API 服务默认注册的业务工具：

`QueryOrderTool`、`QueryLogisticsTool`、`SearchPolicyTool`、`QueryUserHistoryTool`、`CreateRefundTool`、`CreateExchangeOrderTool`、`InterceptShipmentTool`、`SendNotificationTool`、`EscalateToHumanTool`。

初始化数据库：`python scripts/init_aftersales_db.py`。

## 典型用法

```python
from react_ai.tools.base import tool
from react_ai.tools.registry import ToolRegistry
from react_ai.tools.sandbox import ToolSandbox

registry = ToolRegistry(sandbox=ToolSandbox(default_timeout=15.0))
registry.register(calculate)                    # @tool 产物
registry.register(SafeBashTool())               # 内置 BaseTool

result = await registry.execute("calculate", {"expression": "1+1"}, ctx)
```
