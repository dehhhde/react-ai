# Core Engine 核心执行引擎

模块：`react_ai.core` + `react_ai.schemas`。这是 Agent 运行时的心脏，负责驱动 ReAct 状态机、维护执行上下文与生命周期。

## ReActAgent

文件：[core/engine.py](file:///d:/code/react-ai/src/react_ai/core/engine.py)

```python
class ReActAgent(BaseAgent):
    def __init__(
        self,
        llm: BaseLLMClient,
        tool_registry: ToolRegistry,
        config: Optional[AgentConfig] = None,   # 默认 AgentConfig()
        hooks: Optional[List[AgentLifecycleHook]] = None,
    ): ...

    async def run(self, query: str, context: RunContext) -> AgentResponse: ...
    async def stream(self, query: str, context: RunContext) -> AsyncGenerator[AgentEvent, None]: ...
```

- `stream()` 是核心实现，`run()` 内部消费 `stream()` 并在 `RUN_END`/`ERROR` 事件上聚合出 `AgentResponse`。
- 主循环条件：`state.current_step < config.max_steps and not state.is_finished`，每轮先调用 `context.check_cancelled()`。

### 执行循环逻辑

1. **Thought**：发出 `THOUGHT_START`，调用 `llm.generate(messages, tools=registry.list_definitions(), temperature)`。
2. **最终答复分支**：若响应无 `tool_calls`，标记 `is_finished`，记录 `StepResult(is_final=True, final_answer=...)`，发出 `RUN_END`（payload 含 `output` 与全部 `steps`），触发 `on_run_end` 钩子后返回。
3. **Action 分支**：对每个 `tool_call` 依次发出 `TOOL_START` → `tools.execute()`（经沙箱）→ `TOOL_END`（含 `duration_ms`）→ `OBSERVATION`；随后把 assistant 的 `tool_calls` 消息与 `role=tool` 的观察结果追加进 `state.messages`，并记录 `StepResult(thought, action_name, action_input, observation)`。
4. **熔断**：循环耗尽 `max_steps` 仍未完成时发出 `ERROR` 事件：`达到最大步数上限 (N) 熔断退出`。
5. **异常**：任何异常触发 `on_error` 钩子并发出 `ERROR` 事件；工具执行失败先发 `TOOL_ERROR` 再向上抛出。

## RunContext

文件：[core/context.py](file:///d:/code/react-ai/src/react_ai/core/context.py)

```python
class RunContext(BaseModel):
    session_id: str      # 会话ID（uuid4 自动生成）
    run_id: str          # 单次运行 TraceID（uuid4）
    user_id: str | None
    metadata: dict       # 跨环节附加元数据（caller_id/tenant_id/idempotency_key 等）
    budget_tokens: int = 100_000   # 本轮 Token 预算上限
    used_tokens: int = 0
    is_cancelled: bool = False

    def cancel(self) -> None            # 置位取消信号
    def check_cancelled(self) -> None   # 已取消则抛 TimeoutError
```

取消为**协作式**：引擎在每轮循环开头检查，调用方可在另一任务中执行 `ctx.cancel()` 中断长运行。

## AgentState 与 StepResult

- `AgentState`（[core/state.py](file:///d:/code/react-ai/src/react_ai/core/state.py)）：当前步数、消息列表、步骤历史、完成标志、最终输出与错误。
- `StepResult`（[schemas/agent.py](file:///d:/code/react-ai/src/react_ai/schemas/agent.py)）：

```python
class StepResult(BaseModel):
    step_number: int
    thought: str | None
    action_name: str | None
    action_input: dict | None
    observation: str | None
    is_final: bool = False
    final_answer: str | None
```

## AgentConfig

```python
class AgentConfig(BaseModel):
    name: str = "EnterpriseReActAgent"
    description: str = "企业级标准 ReAct 智能体"
    max_steps: int = 15               # 循环熔断上限
    timeout_seconds: float = 120.0
    temperature: float = 0.2
    enable_stream: bool = True
    stop_words: list[str] = []
    system_prompt_override: str | None = None
```

## AgentResponse

```python
class AgentResponse(BaseModel):
    session_id: str
    run_id: str
    final_output: str
    steps: list[StepResult]
    usage: UsageStats          # prompt/completion/total tokens、估算成本
    success: bool = True
    error_message: str | None
```

## 生命周期钩子 AgentLifecycleHook

文件：[core/lifecycle.py](file:///d:/code/react-ai/src/react_ai/core/lifecycle.py)。全部为异步空实现，按需覆写：

| 钩子 | 触发时机 |
|---|---|
| `on_run_start(context, input_query)` | stream 入口 |
| `on_step_start(context, step)` | 每轮迭代开始 |
| `on_tool_call(context, tool_name, args)` | 工具调用前（供外部审计） |
| `on_tool_result(context, tool_name, result)` | 工具返回后 |
| `on_event(context, event)` | 每个事件派发时 |
| `on_run_end(context, output)` | 正常结束 |
| `on_error(context, error)` | 异常终止 |

示例——用钩子统计 Token 与审计：

```python
class AuditHook(AgentLifecycleHook):
    async def on_tool_result(self, context, tool_name, result):
        metrics.record_tool_call(tool_name, success=True)

agent = ReActAgent(llm=llm, tool_registry=registry, hooks=[AuditHook()])
```

## 事件协议 AgentEvent

文件：[schemas/events.py](file:///d:/code/react-ai/src/react_ai/schemas/events.py)

```python
class AgentEvent(BaseModel):
    event_id: str
    event_type: AgentEventType
    step: int = 0
    payload: dict
    timestamp: float
```

`AgentEventType` 全部枚举值（字符串值即 SSE 中 `event_type`）：

| 枚举 | 值 | 含义 |
|---|---|---|
| `RUN_START` | `run_start` | 运行开始，payload: query/session_id |
| `THOUGHT_START` | `thought_start` | 进入思考阶段 |
| `THOUGHT_CHUNK` | `thought_chunk` | 思考增量（流式预留） |
| `ACTION_PLAN` | `action_plan` | 行动规划（预留） |
| `TOOL_START` | `tool_start` | 工具调用开始，payload: tool/args |
| `TOOL_END` | `tool_end` | 工具调用完成，payload: tool/result/duration_ms |
| `TOOL_ERROR` | `tool_error` | 工具执行异常，payload: tool/error |
| `OBSERVATION` | `observation` | 观察结果回写，payload: observation |
| `MESSAGE_CHUNK` | `message_chunk` | 答复增量（流式预留） |
| `RUN_END` | `run_end` | 运行完成，payload: output/steps |
| `ERROR` | `error` | 运行错误，payload: error |

## 消息契约

文件：[schemas/messages.py](file:///d:/code/react-ai/src/react_ai/schemas/messages.py)。`ChatMessage` 对齐 OpenAI 格式：`role`（`system/user/assistant/tool`）、`content`、`tool_calls`、`tool_call_id`、`name`、`metadata`、`timestamp`；另有 `ToolCall`、`ToolCallResult`、`UsageStats`。
