# Orchestration 多智能体编排与工作流

模块：`react_ai.orchestration`（编排器）与 `react_ai.flows`（业务管道）。支持从单 Agent 向多智能体团队演进。

## 编排契约（orchestration/base.py）

文件：[orchestration/base.py](file:///d:/code/react-ai/src/react_ai/orchestration/base.py)

```python
class OrchestratorStatus(Enum):  # PENDING / RUNNING / COMPLETED / FAILED / CANCELLED / TIMEOUT / PARTIAL
class AgentStatus(Enum):         # IDLE / RUNNING / COMPLETED / FAILED / TIMEOUT

@dataclass AgentContext          # 单 Agent 执行上下文：input_data/output_data/status/latency_ms/metadata
@dataclass OrchestratorContext   # 编排全局上下文：orchestration_id/request_id/session_id/shared_state/agent_contexts
@dataclass AgentResult           # agent_id/success/data/error/latency_ms
@dataclass OrchestratorResult    # status/success/data/agent_results/total_latency_ms
```

### 编排用 BaseAgent

注意：编排层有独立的 `react_ai.agents.base.BaseAgent`（`execute(context: AgentContext)` / `execute_async`），与 `react_ai.core.base.BaseAgent`（ReAct 运行时）是两套契约，顶层包将后者别名为 `CoreBaseAgent`。

```python
class MyAgent(BaseAgent):                    # react_ai.agents.base
    name = "summarizer"
    timeout_seconds = 60.0
    max_retries = 2

    def execute(self, context: AgentContext) -> Any:
        return {"summary": context.get("text")[:100]}
```

## BaseOrchestrator 能力

| 能力 | 接口 |
|---|---|
| Agent 管理 | `register_agent(agent, order=None) -> agent_id`、`unregister_agent`、`get_agent`、`list_agents` |
| 执行 | `execute(input_data, ...) -> OrchestratorResult`、`execute_async(...)`、`__call__` |
| 钩子 | `add_pre_hook` / `add_post_hook`（编排级）、`add_agent_pre_hook` / `add_agent_post_hook`（Agent 级） |
| 控制 | `timeout_seconds=300.0` 总超时、`max_retries` + `retry_delay_seconds` 重试、`fail_fast=True` 快速失败、`parallel_limit=10` 并发上限 |
| 统计 | `get_stats()` / `get_metrics()` / `reset_*` |

内置两种开箱即用策略：

```python
from react_ai.orchestration import SequentialOrchestrator, ParallelOrchestrator

pipe = SequentialOrchestrator()          # 按注册 order 串行流转
pipe.register_agent(FetchDataAgent())
pipe.register_agent(AnalyzeAgent())
result = pipe.execute({"url": "https://..."})

fan = ParallelOrchestrator()             # 并发执行全部 Agent（受 parallel_limit 限制）
fan.fail_fast = False                    # 允许部分成功 (PARTIAL)
result = await fan.execute_async(input_data={"query": "..."})
```

## SupervisorAgent 主管-工人模式

文件：[orchestration/supervisor.py](file:///d:/code/react-ai/src/react_ai/orchestration/supervisor.py)

```python
class SupervisorAgent:
    def __init__(self, supervisor_llm, workers: Dict[str, BaseAgent]): ...
    async def execute_task(self, main_task: str, context: RunContext) -> AgentResponse
```

设计语义：主管 LLM 负责任务拆解 → 分发给 Worker（`react_ai.core.base.BaseAgent`，如 ReActAgent）→ 汇总产出。当前实现为返回占位 `AgentResponse` 的**骨架**，拆解/分发/汇总逻辑待接入。

## AgentRouter 意图路由

文件：[orchestration/router.py](file:///d:/code/react-ai/src/react_ai/orchestration/router.py)

```python
router = AgentRouter()
router.register_route("code", code_agent)
router.register_route("support", support_agent)
agent = router.route("帮我修个 bug")   # 当前为简单取首个的占位实现
```

生产接法：以意图分类（轻量 LLM 或规则）替换 `route()` 的占位匹配。

## StateGraphWorkflow 状态图编排

文件：[orchestration/workflow.py](file:///d:/code/react-ai/src/react_ai/orchestration/workflow.py)

```python
wf = StateGraphWorkflow()
wf.add_node("extract", extract_fn)
wf.add_node("classify", classify_fn)
wf.add_edge("extract", "classify")
state = await wf.run({"raw_text": "..."})
```

DAG 节点/边注册接口已定型，拓扑流转执行为占位实现；复杂图驱动场景可直接使用 LangGraph（项目已依赖 `langgraph`，参考 [examples/aftersales_langgraph.py](../examples/aftersales_langgraph.py)）。

## BaseFlow 业务流程管道

文件：[flows/base.py](file:///d:/code/react-ai/src/react_ai/flows/base.py)。面向确定性业务编排（区别于 LLM 自主决策的 Agent）：

```python
class OrderFulfillmentFlow(BaseFlow):
    name = "order_fulfillment_flow"
    version = "1.0.0"
    priority = FlowPriority.HIGH
    timeout_seconds = 30.0

    def build_steps(self):
        self.register_step("validate_order", self.validate_order, description="校验订单")
        self.register_step("risk_check", self.risk_check,
                           condition=lambda ctx: ctx.input_data.get("amount", 0) > 1000)
        self.register_step("lock_inventory", self.lock_inventory, retries=2)
        self.register_step("notify_customer", self.notify_customer)   # 支持 async 步骤
```

要点（完整演示见 [examples/flow_demo.py](../examples/flow_demo.py)）：

- `register_step(step_id, func, **kwargs)`：支持 `condition`（条件跳过）、`retries`（步骤级重试）、`description`。
- 执行：`execute(input_data)` / `execute_async(input_data)` / `__call__`，返回 `FlowResult`（含每步 `StepResult` 与 `FlowStatus`）。
- 钩子：流程级 `add_pre_hook`/`add_post_hook`，步骤级 `add_step_pre_hook`/`add_step_post_hook`。
- 指标：`get_metrics()` / `reset_metrics()` / `reset()`。

## 选型指引

| 场景 | 推荐 |
|---|---|
| 单任务自主推理+工具 | `ReActAgent`（[Core Engine](Core-Engine.md)） |
| 固定步骤业务管道（可重试/条件分支） | `BaseFlow` |
| 多 Agent 串行/并行流水线 | `SequentialOrchestrator` / `ParallelOrchestrator` |
| 按意图分派到专精 Agent | `AgentRouter` |
| 任务拆解-分派-汇总 | `SupervisorAgent`（待完善） |
| 复杂状态图 / checkpoint 持久化 | LangGraph 集成 |
