# 实现任务队列：工具调用 P0 修复

## Task 1: BaseTool.acall 同步 execute 自动 to_thread 卸载

**对应 AC**: AC-1, AC-2
**优先级**: high
**状态**: completed
**Completion Evidence**:
- 代码位置：`src/react_ai/tools/base.py` L399-L405，`acall` 使用 `inspect.iscoroutinefunction(self.execute)` 分流：同步 → `await asyncio.to_thread(self.execute, arguments, ctx)`；异步 → `await self.execute(arguments, ctx)`。
- TR-1.1 通过：`test_sync_execute_runs_in_thread_pool` 断言同步 execute 的线程 ID ≠ 主线程 ID。
- TR-1.2 通过：`test_async_execute_runs_in_event_loop_thread` 断言异步 execute 在主线程执行。
- TR-1.3 通过：`test_tools.py::TestToolBase` 全部 9 项测试通过，异常包装行为不变。

---

## Task 2: ToolSandbox 清理 to_thread 死分支 + 超时生效验证

**对应 AC**: AC-3
**优先级**: high
**状态**: completed
**依赖**: Task 1
**Completion Evidence**:
- 代码位置：`src/react_ai/tools/sandbox.py` L230-L326，`run_safe` 统一走 `tool.acall` + `asyncio.wait_for`，无 `to_thread` 死分支；超时抛 `ToolExecutionError(external/tool_timeout/retryable=True)`。
- TR-2.1 通过：`test_timeout_raises_tool_timeout` 断言 0.3s 内抛 `tool_timeout` 且 `retryable=True`。
- TR-2.2 通过：`test_event_loop_not_blocked_during_timeout` 断言阻塞工具等待期间计数器递增 ≥ 3 次。

---

## Task 3: 引擎并行工具调用（asyncio.gather）

**对应 AC**: AC-4, AC-5
**优先级**: high
**状态**: completed
**依赖**: Task 1, Task 2
**Completion Evidence**:
- 代码位置：`src/react_ai/core/engine.py` L290-L401，`_run_tool_calls` 使用 `asyncio.gather(*[_execute_single_tool_call(...)], return_exceptions=True)` 并行执行；TOOL_START 按序预派发，结果按序处理；审批挂起保存 `tool_calls[idx:]` 到 checkpoint。
- TR-3.1 通过：`test_parallel_execution_faster_than_serial` 断言 3 个各 0.3s 的调用总耗时 < 0.8s。
- TR-3.2 通过：`test_engine.py` 与 `test_hitl_approval.py` 验证多 tool_calls 消息协议正确。
- TR-3.3 通过：`test_hitl_approval.py::TestEnginePauseResume` 验证审批挂起 checkpoint 完整。
- TR-3.4 通过：`test_tool_error_feedback.py::test_consecutive_failures_circuit_breaker` 验证连续失败熔断。

---

## Task 4: 取消传播

**对应 AC**: AC-14
**优先级**: medium
**状态**: completed
**依赖**: Task 3
**Completion Evidence**:
- 代码位置：`src/react_ai/core/context.py` L18-L23 定义 `cancel()`/`check_cancelled()`；`sandbox.py` L266-L267 执行前 `context.check_cancelled()`；`engine.py` L213 步边界检查。
- TR-4.1 通过：`test_cancelled_context_stops_loop` 断言 `ctx.cancel()` 后 `_loop` 不发起 LLM 调用（`llm.call_count == 0`）。

---

## Task 5: SafeBashTool 白名单 + shell=False + 输出截断

**对应 AC**: AC-6, AC-7, AC-8
**优先级**: high
**状态**: completed
**Completion Evidence**:
- 代码位置：`src/react_ai/tools/builtin/bash.py`，`ALLOWED_COMMANDS` 白名单、`shlex.split` 解析、`shell=False` 数组化执行、`FORBIDDEN_META` 元字符拦截、`MAX_OUTPUT_BYTES=8KB` 截断、环境隔离。
- TR-5.1 通过：`test_whitelist_blocks_rm`（rm → auth 拒绝）、`test_whitelist_allows_echo`（echo 成功）。
- TR-5.2 通过：`test_shell_false_and_args_list` 断言 `shell=False` 且 args 为 list。
- TR-5.3 通过：`test_output_truncation` 断言 10KB 输出被截断至 ≤ 8KB + marker。

---

## Task 6: HttpTool SSRF 防护 + 响应限流

**对应 AC**: AC-9, AC-10
**优先级**: high
**状态**: completed
**Completion Evidence**:
- 代码位置：`src/react_ai/tools/builtin/http.py`，scheme 白名单(http/https)、`_resolve_host`+`_is_blocked_ip` SSRF 封禁(覆盖 127/10/172.16/192.168/169.254/IPv6 内网等)、`ALLOWED_METHODS`、`FORBIDDEN_HEADERS` 敏感头过滤、`httpx.stream` 流式读取 + `MAX_RESPONSE_BYTES=1MB` 截断、分层超时。
- TR-6.1 通过：`test_blocks_internal_ips`（127.0.0.1/169.254.169.254/10.0.0.1/192.168.1.1 均 auth 拒绝）、`test_blocks_non_http_scheme`（file:// 拒绝）。
- TR-6.2/6.3：代码审查确认流式截断逻辑（L96-L101）与公网放行路径。

---

## Task 7: 工具输出 PII 脱敏 + 注入扫描

**对应 AC**: AC-11, AC-12
**优先级**: high
**状态**: completed
**Completion Evidence**:
- 代码位置：`src/react_ai/guardrails/tool_output.py` 实现 `sanitize_observation`（PIIMaskingGuardrail + PromptInjectionGuardrail 链，BLOCK 时返回兜底文本）；`engine.py` L359-L360 对 Observation 调用 `_serialize_observation` + `_apply_output_guardrails`。
- TR-7.1 通过：`test_pii_masking` 断言 13800138000 → [PHONE_MASKED]。
- TR-7.2 通过：`test_injection_blocked` 断言注入文本被替换为安全兜底。
- TR-7.3：代码审查确认 `_apply_output_guardrails` 用 try/except 包 `sanitize_observation`，异常时返回原文（fail-open）。

---

## Task 8: 现有测试兼容性验证与回归

**对应 AC**: AC-13
**优先级**: high
**状态**: completed
**依赖**: Task 1-7
**Completion Evidence**:
- 运行 `pytest tests/unit/test_tools.py tests/unit/test_engine.py tests/unit/test_tool_error_feedback.py tests/unit/test_hitl_approval.py tests/unit/test_guardrails.py tests/unit/test_idempotency.py tests/unit/test_events.py tests/unit/test_agents.py` → **90 passed**。
- 新增 `tests/unit/test_spec_verification.py` → **14 passed**（覆盖全部 14 条 AC）。
- `test_memory.py` 因缺少 `fakeredis` 依赖无法收集（与本次任务无关，为环境问题）。
- AC-13 评分：4/4（全部通过）。
