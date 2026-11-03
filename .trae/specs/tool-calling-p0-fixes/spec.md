# 工具调用 P0 修复规范：执行模型与安全隔离

> 对应差距分析报告 `docs/TOOL_CALLING_GAP_ANALYSIS.md` 第 2.5 节（执行模型与并发）与第 2.6 节（安全隔离）。

## 1. 问题

当前工具调用模块在两个 P0 维度上存在生产阻断性风险：

1. **同步工具阻塞事件循环**：`BaseTool.acall` 内部直接同步调用 `self.execute`（`base.py` L318），而售后工具全部是同步 psycopg 数据库操作。这意味着工具调用在 FastAPI 事件循环线程里直接阻塞执行，`asyncio.wait_for` 无法取消阻塞中的同步调用，超时隔离对同步工具实际无效。`sandbox.run_safe` 的 `to_thread` 分支（L45-L48）因 `acall` 恒存在而永远不可达。

2. **安全隔离形同虚设**：
   - `SafeBashTool` 仅用 4 个字符串黑名单 + `shell=True`，`rm -fr`、`rm  -rf`、base64 管道、`python -c` 等全部可绕过；stdout/stderr 无长度截断。
   - `HttpTool` 无 URL 校验 → SSRF 直通内网/云元数据地址；无方法白名单；`resp.text` 先全量下载再截断 → 大响应打爆内存。
   - 工具输出（Observation）直接 `str()` 后进入下一轮 prompt，未经过 PII 脱敏与 prompt 注入扫描，检索/HTTP 等外部数据源是注入攻击的主要载体。

## 2. 用户与目标

**用户**：框架维护者与在生产环境部署该 Agent 的工程团队。

**目标**：
- 让同步工具的执行不再阻塞 FastAPI 事件循环，使超时隔离真正生效。
- 让 LLM 一次返回多个 `tool_calls` 时能并行执行，降低多查询场景延迟。
- 让运行中的工具调用可被取消（取消传播到线程）。
- 让 `SafeBashTool` 和 `HttpTool` 达到企业级安全基线（白名单、SSRF 封禁、输出截断）。
- 让工具输出在进入上下文前经过 PII 脱敏与注入扫描。

## 3. 非目标

- 不改造 HITL 审批链路（已存在审批服务与 checkpoint 机制）。
- 不改造幂等去重存储（已存在 idempotency guard/store）。
- 不改造错误回喂机制（引擎已实现按 error_type 差异化回喂）。
- 不引入 asyncpg 替换 psycopg（仅通过 `to_thread` 卸载同步调用，避免大规模改写 9 个售后工具）。
- 不实现完整 RBAC/ABAC 鉴权服务（仅预留授权钩子点）。
- 不做流式 tool call 增量参数拼接（属 P2 竞争力项）。

## 4. 功能需求

### 4.1 同步工具自动卸载到线程池（FR-1）

`BaseTool.acall` 必须检测 `execute` 是否为协程函数：
- 若 `execute` 是 `async def`，直接 `await self.execute(...)`。
- 若 `execute` 是同步函数，通过 `asyncio.to_thread(self.execute, ...)` 卸载到线程池执行，避免阻塞事件循环。

`ToolSandbox.run_safe` 的 `to_thread` 分支应被移除或统一为走 `acall`（因为 `acall` 已内部处理同步/异步），消除"acall 恒存在导致 to_thread 不可达"的死代码。

### 4.2 超时隔离对同步工具生效（FR-2）

`ToolSandbox.run_safe` 使用 `asyncio.wait_for` 包裹 `acall`。由于同步 `execute` 已通过 `to_thread` 卸载，`wait_for` 超时会取消协程（线程内同步调用无法被强杀，但协程层面超时会抛出 `TimeoutError`，不会无限阻塞事件循环）。超时结果以 `external/tool_timeout/retryable=True` 的 `ToolExecutionError` 抛出，供引擎回喂模型。

### 4.3 并行工具调用（FR-3）

引擎 `_run_tool_calls` 在 LLM 返回多个 `tool_calls` 时，当前逐个串行执行。改造为：
- 使用 `asyncio.gather` 并行执行所有工具调用。
- 每个工具调用独立产生 `TOOL_START`/`TOOL_END`/`TOOL_ERROR`/`OBSERVATION` 事件。
- 并行执行的结果全部收集后，按 tool_calls 顺序统一 append 到消息历史（保证消息协议正确）。
- 若任一调用触发 `ApprovalRequiredError`，需保存包含所有未完成 tool_calls 的 checkpoint 并挂起。
- 若任一调用触发熔断（`ToolFailureBudgetExceeded`），按现有逻辑终止 run。

### 4.4 取消传播（FR-4）

`RunContext.check_cancelled` 目前只在步边界调用。新增：
- `ToolSandbox.run_safe` 在执行前/后检查取消标志；若已取消则抛出 `CancelledError`。
- 引擎 `_loop` 在 `asyncio.gather` 并行执行工具时，若 `context.cancelled` 为真，通过 `asyncio.shield` + 标志位优雅停止（不强杀线程，但不再等待结果，直接熔断退出）。

### 4.5 SafeBashTool 白名单与安全执行（FR-5）

将 `SafeBashTool` 从黑名单模式改为白名单 + 参数数组化：
- 维护可执行命令白名单（如 `echo`、`ls`、`cat`、`grep`、`pwd`、`date`、`whoami`、`wc` 等只读/安全命令）。
- 禁止 `shell=True`，改用 `subprocess.run([cmd, *args], shell=False, ...)`。
- 命令解析使用 `shlex.split`，若首个 token 不在白名单则拒绝执行（返回 `auth/permission_denied`）。
- stdout/stderr 截断：合并输出最大 8KB，超出截断并附 `...[truncated]`。
- 保留 `requires_approval=True` 与 `side_effects="destructive"`。
- 超时沿用 `timeout_seconds`（默认 30s，bash 工具内 `subprocess.run` 也设 timeout）。

### 4.6 HttpTool SSRF 防护与响应限流（FR-6）

`HttpTool` 增加以下安全控制：
- **URL 校验**：解析 URL，禁止访问内网/回环/链路本地/云元数据地址（`127.0.0.0/8`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`169.254.0.0/16`、`::1`、`fc00::/7`、`fe80::/10`），禁止 `file://`、`gopher://` 等非 http(s) scheme。解析目标主机的所有 A/AAAA 记录，若任一落在上述范围则拒绝（防 DNS rebinding 的第一道防线）。
- **方法白名单**：仅允许 `GET`、`POST`、`PUT`、`DELETE`、`PATCH`、`HEAD`，默认 `GET`。
- **流式响应上限**：使用 `httpx.stream` 流式读取，累计超过 1MB（可配）则停止读取并截断，防止大响应打爆内存。
- **超时分层**：连接超时 5s，读取超时 20s。
- 拒绝时返回 `auth/permission_denied` 或 `schema/invalid_arguments`，不发起实际请求。

### 4.7 工具输出安全扫描（FR-7）

在引擎将工具结果转为 Observation 前，经过 guardrails 扫描：
- 在 `ToolSandbox` 或引擎 `_run_tool_calls` 中，对成功的工具输出（`tool_result.data` 的字符串表示）执行 PII 脱敏与 prompt 注入扫描。
- PII 脱敏（`PIIMaskingGuardrail`）：对手机号、身份证号做掩码替换，修改后内容作为 Observation。
- 注入扫描（`PromptInjectionGuardrail`）：若检测到注入特征，将 Observation 替换为安全兜底内容（如"[工具输出因安全策略被过滤]"），并记录日志。
- 扫描失败（guardrail 异常）时默认放行原始内容，不阻断主流程（fail-open 策略），但记录错误日志。
- 通过 `settings.security.enable_guardrails` 控制是否启用。

## 5. 非功能需求

- **性能**：同步工具经 `to_thread` 卸载后，事件循环不再被阻塞；并行工具调用使 N 个独立查询的延迟从 `sum(t_i)` 降为 `max(t_i)`。
- **兼容性**：不改变 `BaseTool.call`/`acall`/`execute` 的公开签名；现有 9 个售后工具无需修改即可受益于 `to_thread` 卸载。
- **可测试性**：所有安全控制（白名单、SSRF 封禁、输出截断、PII/注入扫描）均可被单元测试覆盖，不依赖真实网络或 subprocess。
- **可配置性**：bash 白名单、http 响应上限、内网地址段等通过类常量或 settings 暴露，便于运维调整。

## 6. 约束与假设

- Python >= 3.10（`asyncio.to_thread` 可用）。
- `httpx` 已在依赖中（`pyproject.toml` L15）。
- 不引入新依赖（`shlex`、`ipaddress`、`socket` 均为标准库）。
- 现有测试（`test_tools.py`、`test_engine.py`、`test_tool_error_feedback.py`）必须继续通过。

## 7. 开放问题

- FR-3 并行执行时，多个工具调用的事件流交错是否会影响前端 SSE 渲染？假设前端按 `step` 字段分组渲染即可。
- FR-7 的注入扫描对 Observation 执行是否会误杀正常业务数据？假设 `PromptInjectionGuardrail` 的模式足够保守，且 fail-open 策略兜底。

## 8. 验收标准

### AC-1（rule）：同步 execute 经 to_thread 卸载
- 给定一个同步 `execute` 的工具，当通过 `acall` 调用时，执行发生在线程池中而非事件循环线程。
- 验证方式：单元测试中在 `execute` 内打印 `threading.current_thread().name`，断言不等于事件循环线程名（可用 `asyncio.get_running_loop` 配合）。

### AC-2（rule）：acall 对异步 execute 直接 await
- 给定一个 `async def execute` 的工具，当通过 `acall` 调用时，不经过 `to_thread`，直接 await。
- 验证方式：单元测试断言异步 execute 在事件循环线程执行。

### AC-3（rule）：超时隔离生效
- 给定一个同步 `execute` 内 `time.sleep(5)` 的工具，当 `timeout_seconds=1` 时，`run_safe` 在约 1s 后抛出 `ToolExecutionError`（error_type=external, error_code=tool_timeout, retryable=True），且事件循环在等待期间仍可处理其他任务。
- 验证方式：单元测试用 `asyncio.gather` 并发运行一个计时器与工具调用，断言计时器递增未被阻塞。

### AC-4（rule）：并行工具调用
- 给定 LLM 一次返回 3 个独立 tool_calls（每个 execute sleep 0.5s），引擎总耗时约 0.5s 而非 1.5s。
- 验证方式：单元测试断言 `_run_tool_calls` 耗时 < 1.0s，且 3 个 OBSERVATION 事件均产出。

### AC-5（rule）：并行执行时任一审批挂起保存完整 checkpoint
- 给定 3 个 tool_calls，第 2 个触发 `ApprovalRequiredError`，checkpoint 中 `pending_tool_calls` 包含第 2、3 个调用。
- 验证方式：单元测试 mock 审批服务抛错，断言 checkpoint 内容。

### AC-6（rule）：SafeBashTool 白名单拦截
- 给定命令 `rm -rf /`，`SafeBashTool.execute` 返回失败（auth/permission_denied），不执行 subprocess。
- 给定命令 `echo hello`，执行成功且 stdout 含 `hello`。
- 验证方式：单元测试。

### AC-7（rule）：SafeBashTool 不使用 shell=True
- 给定任意命令，`SafeBashTool.execute` 调用 `subprocess.run` 时 `shell=False`。
- 验证方式：单元测试 mock `subprocess.run`，断言 `shell=False` 且 args 为列表。

### AC-8（rule）：SafeBashTool 输出截断
- 给定命令产生 10KB stdout，返回的 stdout 长度 <= 8KB + 截断标记。
- 验证方式：单元测试 mock subprocess 返回大输出。

### AC-9（rule）：HttpTool SSRF 封禁
- 给定 URL `http://127.0.0.1/admin`、`http://169.254.169.254/latest/meta-data/`、`http://10.0.0.1/`、`file:///etc/passwd`，均返回失败且不发起 HTTP 请求。
- 验证方式：单元测试 mock `httpx.Client`，断言 `request` 未被调用。

### AC-10（rule）：HttpTool 响应大小上限
- 给定返回 2MB 响应的 URL，工具返回的 text 长度 <= 1MB + 截断标记，且不抛出 MemoryError。
- 验证方式：单元测试 mock httpx 流式响应。

### AC-11（rule）：工具输出 PII 脱敏
- 给定工具返回包含手机号 `13800138000` 的数据，Observation 中手机号被替换为 `[PHONE_MASKED]`。
- 验证方式：单元测试集成 `PIIMaskingGuardrail`，断言 Observation 内容。

### AC-12（rule）：工具输出注入扫描
- 给定工具返回包含 "ignore previous instructions" 的数据，Observation 被替换为安全兜底内容。
- 验证方式：单元测试集成 `PromptInjectionGuardrail`，断言 Observation 不含原始注入内容。

### AC-13（rubric）：现有测试兼容性
- 维度：现有单元测试通过率
- 1-4 分：4=全部通过，3=1 个失败且为预期行为变更，2=2-3 个失败，1=4+ 失败
- 阈值：>= 3
- 验证方式：运行 `pytest tests/unit/test_tools.py tests/unit/test_engine.py tests/unit/test_tool_error_feedback.py`。

### AC-14（rule）：取消传播
- 给定运行中的工具调用（execute 内 sleep），当 `context.cancel()` 被调用时，引擎在步边界检测到取消并停止，不再发起新的 LLM 调用。
- 验证方式：单元测试设置 `context.cancelled=True`，断言循环退出。
