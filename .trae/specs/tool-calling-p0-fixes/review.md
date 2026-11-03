# 独立审查：工具调用 P0 修复

## 审查范围

对差距分析报告 `docs/TOOL_CALLING_GAP_ANALYSIS.md` 第 2.5（执行模型与并发）与 2.6（安全隔离）节的 P0 修复实现进行独立审查。

## 验收标准核查表

| AC | 类型 | 描述 | 审查状态 | 证据 |
|---|---|---|---|---|
| AC-1 | rule | 同步 execute 经 to_thread 卸载 | ✅ pass | base.py L399-405; test_spec_verification.py::test_sync_execute_runs_in_thread_pool |
| AC-2 | rule | acall 对异步 execute 直接 await | ✅ pass | base.py L400-401; test_async_execute_runs_in_event_loop_thread |
| AC-3 | rule | 超时隔离生效且不阻塞事件循环 | ✅ pass | sandbox.py L296-306; test_timeout_raises_tool_timeout, test_event_loop_not_blocked_during_timeout |
| AC-4 | rule | 并行工具调用 | ✅ pass | engine.py L307 gather; test_parallel_execution_faster_than_serial |
| AC-5 | rule | 并行执行审批挂起保存完整 checkpoint | ✅ pass | engine.py L332-346; test_parallel_approval_saves_remaining_calls |
| AC-6 | rule | SafeBashTool 白名单拦截 | ✅ pass | bash.py L71-75; test_whitelist_blocks_rm |
| AC-7 | rule | SafeBashTool 不使用 shell=True | ✅ pass | bash.py L108; test_shell_false_and_args_list |
| AC-8 | rule | SafeBashTool 输出截断 | ✅ pass | bash.py L130-134; test_output_truncation |
| AC-9 | rule | HttpTool SSRF 封禁 | ✅ pass | http.py L73-79, L128-155; test_blocks_internal_ips |
| AC-10 | rule | HttpTool 响应大小上限 | ✅ pass | http.py L96-101; test_response_size_truncation |
| AC-11 | rule | 工具输出 PII 脱敏 | ✅ pass | tool_output.py; engine.py L360; test_pii_masking |
| AC-12 | rule | 工具输出注入扫描 | ✅ pass | tool_output.py L79-87; test_injection_blocked |
| AC-13 | rubric | 现有测试兼容性 (阈值 >=3) | ✅ pass (4/4) | 106 passed (16 verification + 90 existing) |
| AC-14 | rule | 取消传播 | ✅ pass | context.py L18-23, sandbox.py L266, engine.py L213; test_cancelled_context_stops_loop |

## 审查历史

### 第一轮审查（独立审查者）

**结果**: PASS（附带可处理发现）

**发现**:
1. [中等] `enable_guardrails` 开关未接入引擎 `_apply_output_guardrails`（违反 FR-7）
2. [低] AC-10 缺少专项单元测试
3. [低] AC-5 缺少并行审批 checkpoint 专项测试
4. [低] AC-14 并行 gather 期间无 shield+标志位优雅停止（非阻塞，行为安全）
5. [低] `find` 命令在白名单内可执行 `-delete` 破坏性动作
6. [低] HTTP 读取超时 15s（spec 建议 20s）、方法白名单含 OPTIONS（spec 未列）

### 修复（实现者）

- **Finding #1（已修复）**: engine.py `_apply_output_guardrails` 增加 `if not settings.security.enable_guardrails: return text`。
- **Finding #2（已修复）**: 新增 `test_response_size_truncation`，mock httpx 流式 2MB 响应，断言截断至 ≤ 1MB。
- **Finding #3（已修复）**: 新增 `test_parallel_approval_saves_remaining_calls`，验证 3 并行调用中第 2 个触发审批时 checkpoint 含第 2、3 个调用。
- **Finding #5（已修复）**: 从 bash 白名单移除 `find` 命令（因支持 `-delete`/`-exec` 破坏性动作）。
- Finding #4、#6 为低优先级非阻塞项，留待后续迭代。

### 修复后验证

- `pytest tests/unit/test_spec_verification.py` → 16 passed
- `pytest tests/unit/test_spec_verification.py tests/unit/test_tools.py tests/unit/test_engine.py tests/unit/test_tool_error_feedback.py tests/unit/test_hitl_approval.py tests/unit/test_guardrails.py tests/unit/test_idempotency.py tests/unit/test_events.py tests/unit/test_agents.py` → 106 passed

## 最终判定

**PASS** — 全部 14 条 AC 均通过独立验证，所有可处理发现已修复或明确为非阻塞低优先级项。核心 P0 修复（to_thread 卸载、超时隔离、并行 gather、SSRF 封禁、bash 白名单+shell=False+截断、PII/注入护栏、enable_guardrails 开关）全部正确实现并通过自动化测试。
