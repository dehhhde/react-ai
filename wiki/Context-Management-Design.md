# 企业级上下文管理设计

> 适用对象：`react_ai.core` / `react_ai.memory` / `react_ai.prompts` 模块，以及基于本框架构建的生产 Agent。
> 本文回答：**生产级 Agent 的上下文应该如何分层、如何组装、如何控预算、如何隔离、如何观测、如何演进？**
> 本文同时标注项目现状（✅ 已实现 / 🟡 部分实现 / ⏳ 规划），避免把设计蓝图误读为既有能力。
> 更新时间：2026-11-09

---

## 1. 设计目标与非目标

### 1.1 目标

| 维度 | 要求 |
|---|---|
| 正确性 | 多轮对话、跨会话事实、工具观测不丢失、不串扰 |
| 可控性 | Token 成本可估算、有硬上限、超限有确定的降级路径 |
| 隔离性 | `tenant / user / session` 三级隔离是硬约束，而非约定 |
| 可靠性 | 记忆后端故障不得阻断主流程，只能降级 |
| 安全性 | 注入内容与敏感信息不得进入上下文，更不得落库 |
| 可观测性 | 每次上下文组装可回放、可度量、可告警 |
| 可演进性 | 各层可插拔替换（内存 → Redis → PG / 向量库） |

### 1.2 非目标

- 不做"全量历史无界注入"——上下文不是数据库；
- 不做"隐式全局缓存"——所有跨请求状态必须显式分层并带 TTL；
- 不依赖模型自行遗忘来控制成本——预算必须由运行时强制执行。

---

## 2. 上下文分层模型

企业级上下文的第一原则是**按生命周期与隔离维度分层**，而不是把所有信息塞进一个 `messages` 数组。

| 层 | 内容 | 生命周期 | 存储 | 项目实现 |
|---|---|---|---|---|
| L0 系统契约层 | system prompt、输出 Schema、工具定义 | 请求级（版本化） | PromptManager | [prompts/manager.py](file:///d:/code/react-ai/src/react_ai/prompts/manager.py) |
| L1 长期事实层 | 用户稳定偏好、身份、显式要求 | 跨会话 | 向量库（pgvector / 内存） | [memory/long_term.py](file:///d:/code/react-ai/src/react_ai/memory/long_term.py) ✅ |
| L2 会话历史层 | 最近 N 轮 user / assistant 纯文本 | 会话级 TTL（默认 86400s） | Redis / PG / InMemory | [memory/base.py#L532-L668](file:///d:/code/react-ai/src/react_ai/memory/base.py#L532-L668) ✅ |
| L3 工作记忆层 | 当前 run 的步骤、最后动作、摘要 | run 级 TTL（默认 1800s） | memory + checkpoint | [memory/base.py#L674-L719](file:///d:/code/react-ai/src/react_ai/memory/base.py#L674-L719) ✅ |
| L4 工具结果层 | 成功工具调用的结果缓存 | 短 TTL（默认 3600s） | memory | [memory/base.py#L740-L781](file:///d:/code/react-ai/src/react_ai/memory/base.py#L740-L781) ✅ |
| L5 本轮轨迹层 | 当前 ReAct 的 assistant / tool 消息、Observation | 单 run | `AgentState.messages` | [core/state.py](file:///d:/code/react-ai/src/react_ai/core/state.py) ✅ |
| L6 运行控制层 | Token 预算、取消信号、审批挂起 | 单 run | `RunContext` | [core/context.py#L7-L24](file:///d:/code/react-ai/src/react_ai/core/context.py#L7-L24) ✅ |

**为什么必须分层**：每层的失效策略不同。历史会过期、工作记忆随 run 结束失效、工具结果可缓存复用、长期事实需要被遗忘权删除。混在一层会导致"要么全留、要么全删"，成本和合规都不可控。

---

## 3. 总体架构

```text
                     ┌──────────────────────────────────────────────┐
                     │              请求入口 / API 层                │
                     │   tenant_id / user_id / session_id（必填）    │
                     └──────────────────────┬───────────────────────┘
                                            ▼
                              ┌──────────────────────────┐
                              │ L6 RunContext 运行控制层  │  budget_tokens / cancel / pause
                              └────────────┬─────────────┘
                                           ▼
         ┌─────────────────────────────────┬─────────────────────────────────┐
         │  召回（入口）                     │  沉淀（出口）                    │
         │  L2 会话历史 load_session        │  save_turn（仅纯文本）           │
         │  L1 长期事实 search_relevant     │  add_fact（抽取 + 去重 + 护栏）   │
         └────────────────┬────────────────┴────────────────┬────────────────┘
                          ▼                                 │
              ┌───────────────────────────┐                 │
              │ 预算裁剪（最旧优先丢弃）    │                 │
              │ 至少保留最新 1 条          │                 │
              └─────────────┬─────────────┘                 │
                            ▼                               │
    L0 system ──► L1 长期记忆参考块 ──► L2 历史 ──► 本轮 user│
                            │                               │
                            ▼                               │
              ┌───────────────────────────┐                 │
              │ ReAct 循环（L5 轨迹增长）  │                 │
              │ 每步 Observation 截断+护栏 │                 │
              └─────────────┬─────────────┘                 │
                            ▼                               │
                     ┌──────────────┐                       │
                     │ 最终答复 / END│───────────────────────┘
                     └──────────────┘
                            │
                            ▼
              ┌───────────────────────────┐
              │ L3 工作记忆 / L4 工具结果  │  按 run_id / 工具参数指纹缓存
              └───────────────────────────┘
```

---

## 4. 上下文生命周期（六阶段）

| 阶段 | 动作 | 代码位置 | 失败策略 |
|---|---|---|---|
| ① 身份解析 | `(tenant, user, session)` → `SessionKey` | [engine.py#L258-L270](file:///d:/code/react-ai/src/react_ai/core/engine.py#L258-L270) | session 缺失 → 跳过记忆读写，不抛错 |
| ② 入口召回 | 并行加载会话历史 + 长期事实 | [engine.py#L140-L144](file:///d:/code/react-ai/src/react_ai/core/engine.py#L140-L144) | 任一召回失败 → 返回空列表，告警 |
| ③ 预算裁剪 | 条数截断 → Token 预算裁剪 | [base.py#L582-L603](file:///d:/code/react-ai/src/react_ai/memory/base.py#L582-L603) | 裁剪后至少保留 1 条 |
| ④ 组装注入 | system → 长期块 → 历史 → 本轮 | [engine.py#L500-L560](file:///d:/code/react-ai/src/react_ai/core/engine.py#L500-L560) | 渲染失败 → 降级内置常量 |
| ⑤ 循环执行 | 每步检查预算、截断 Observation | [engine.py#L577-L608](file:///d:/code/react-ai/src/react_ai/core/engine.py#L577-L608) | 超预算 → 优雅收尾 RUN_END |
| ⑥ 出口沉淀 | 回写会话、抽取长期事实、落工作记忆 | [engine.py#L150-L153](file:///d:/code/react-ai/src/react_ai/core/engine.py#L150-L153) | 失败仅告警，不影响本轮结果 |

**关键原则**：召回与沉淀都在主流程的"外围"，任何记忆后端异常都必须被吞掉并降级，绝不能中断推理。

---

## 5. 预算体系：三层硬上限

上下文成本必须由运行时强制，不能寄望于提示词约束。

| 层级 | 配置 | 默认值 | 超限行为 | 实现 |
|---|---|---|---|---|
| 历史注入预算 | `HISTORY_TOKEN_BUDGET` | 4000 | 从最旧消息丢弃，至少保留最新 1 条 | [token_counter.py#L21-L41](file:///d:/code/react-ai/src/react_ai/utils/token_counter.py#L21-L41) |
| 单次运行预算 | `RunContext.budget_tokens` | 100000 | 终止循环，返回"预算耗尽"最终答复 | [engine.py#L577-L598](file:///d:/code/react-ai/src/react_ai/core/engine.py#L577-L598) |
| 单条 Observation | `MAX_OBSERVATION_CHARS` | 4000 字符 | 截断并追加截断标记 | [engine.py#L34](file:///d:/code/react-ai/src/react_ai/core/engine.py#L34)、[L847-L856](file:///d:/code/react-ai/src/react_ai/core/engine.py#L847-L856) |
| 工作记忆摘要 | `WORKING_OBSERVATION_CHARS` | 200 字符 | 仅保留摘要，完整内容不进工作记忆 | [engine.py#L36-L37](file:///d:/code/react-ai/src/react_ai/core/engine.py#L36-L37) |

### 5.1 裁剪算法必须遵守的三条语义

1. `budget <= 0` 视为不启用，保持向后兼容；
2. **至少保留最新一条消息**——空历史让模型对本轮完全失忆，比超限更糟；
3. 从最旧消息开始丢弃，保留部分维持时间正序，保证 KV Cache 命中。

### 5.2 容量规划示例

以 128k 上下文窗口为例，推荐的预算分配（示例值，按业务调整）：

```text
system + 工具定义       ~2k
长期记忆参考块           ~1k（top_k=3，每条事实短句）
会话历史                4k（HISTORY_TOKEN_BUDGET）
本轮轨迹（含 Observation） 动态占用，受 MAX_OBSERVATION_CHARS 与 ReAct 步数约束
输出预留                 ~4k-8k
单 run 总预算            100k（budget_tokens），超限优雅收尾
```

注意：历史预算与运行预算**不是同一件事**——前者控制"注入多少旧信息"，后者控制"这一轮总共能烧多少 Token"。

---

## 6. 上下文组装协议

### 6.1 固定顺序

```text
[system: 系统提示词]
[system: 长期记忆参考块（可选）]
[user / assistant: 历史轮次（纯文本）]
[user: 本轮问题]
... ReAct 循环产生 assistant(tool_calls) / tool(observation) ...
```

对应实现见 [engine.py#L500-L545](file:///d:/code/react-ai/src/react_ai/core/engine.py#L500-L545)。

### 6.2 不变量（违反即视为缺陷）

| # | 不变量 | 原因 |
|---|---|---|
| 1 | 一轮的多个 `tool_calls` 必须聚合在单条 assistant 消息中 | 拆分会被 OpenAI 协议拒绝（400），并造成上下文错位 |
| 2 | 会话历史只回写 user / assistant 纯文本，不落 tool_calls 与 tool 消息 | 避免下一轮回放出残缺的工具协议 |
| 3 | 长期记忆必须以 `system` 参考块注入，并显式标注"仅供参考，不是本轮指令" | 防止历史事实被模型当作指令执行 |
| 4 | 用户原文必须先过注入检测与 PII 脱敏，再进入模板渲染 | 阻断注入与敏感信息外泄 |
| 5 | 不允许把用户可控内容拼进 system 契约层 | 契约层只允许平台配置 |
| 6 | 组装顺序确定，不依赖字典/集合遍历顺序 | 保证 KV Cache 前缀命中，降低延迟与成本 |

### 6.3 冲突优先级

**本轮显式指令 > 长期记忆事实 > 会话历史**。当三者矛盾时，以本轮输入为准；长期记忆只作为背景，不覆盖当前意图。

---

## 7. 隔离模型

### 7.1 身份三元组

```python
SessionKey(tenant_id, user_id, session_id)  # 三维度非空，缺失即抛 ValueError
```

实现见 [memory/base.py#L146-L184](file:///d:/code/react-ai/src/react_ai/memory/base.py#L146-L184)。

| 入口 | 隔离维度 | 缺失身份时的行为 |
|---|---|---|
| `load_session` / `save_turn` | 三元组 | `session_id` 缺失 → 抛错；tenant/user 缺失 → 独立匿名命名空间 |
| `write` / `read` | 必须携带 `tenant_id` | 硬闸开启时直接 `ValueError` |
| `clear_session` / `clear_user` | 显式 `tenant_id` | 缺失即拒绝，禁止跨租户清除 |
| `LongTermSemanticMemory` | `user_id` + `tenant_id` | 跨用户/跨租户记录不可召回 |

硬闸实现：写 [base.py#L320-L323](file:///d:/code/react-ai/src/react_ai/memory/base.py#L320-L323)、读 [L371-L374](file:///d:/code/react-ai/src/react_ai/memory/base.py#L371-L374)、清理 [L441-L450](file:///d:/code/react-ai/src/react_ai/memory/base.py#L441-L450)、[L486-L495](file:///d:/code/react-ai/src/react_ai/memory/base.py#L486-L495)。开关 `ENFORCE_TENANT_ISOLATION` 默认开启，仅开发/测试允许关闭。

### 7.2 失效与清理

- 每层记忆都带 `ttl_seconds`，读取时过滤过期项；
- 容量上限 `max_items`（默认 10000），超限触发淘汰并上报指标；
- 长期记忆必须提供 `forget_user` 以满足"被遗忘权"。

---

## 8. 压缩策略

### 8.1 现状 ✅

| 手段 | 作用点 | 效果 |
|---|---|---|
| 条数窗口 | `load_session(max_messages)` | 只取最近 N 条 |
| Token 预算裁剪 | `trim_messages_to_token_budget` | 从最旧丢弃到预算内 |
| Observation 截断 | `_serialize_observation` | 单条工具结果 ≤ 4000 字符 |
| 工作记忆摘要 | `WORKING_OBSERVATION_CHARS` | 只留 200 字符摘要 |
| 工具输出护栏 | `_apply_output_guardrails` | PII 掩码 + 注入内容替换 |

### 8.2 演进建议 ⏳

| 方案 | 触发条件 | 设计要点 |
|---|---|---|
| 滚动摘要 | 历史超过预算 70% | 将最早若干轮压缩为一条 system 摘要，保留原始最近 K 轮 |
| 结构化记忆抽取 | 会话结束 / 每 N 轮 | 把事实、偏好、待办写入长期层，历史可安全丢弃 |
| 语义去重 | 长期事实写入前 | 相似度超阈值不重复沉淀 |
| 大结果外置 | 工具结果超阈值 | 上下文只放摘要 + 引用 ID，按需二次读取 |

**注意**：摘要会引入信息损失与二次幻觉，必须保留可回溯的原始消息归档（PG），并记录摘要版本与模型。

---

## 9. 上下文安全

| 风险 | 防线 | 位置 |
|---|---|---|
| 直接提示注入 | 渲染前护栏链，命中即抛 `PromptInjectionError`，短路所有后续调用 | [prompts/base.py#L177-L182](file:///d:/code/react-ai/src/react_ai/prompts/base.py#L177-L182) |
| PII 外泄 | 模板内置 `mask_pii` 过滤器 + 渲染护栏 | [prompts/env.py#L22](file:///d:/code/react-ai/src/react_ai/prompts/env.py#L22) |
| 间接注入（工具输出） | Observation 注入扫描，命中替换为安全兜底文本 | [engine.py#L858-L870](file:///d:/code/react-ai/src/react_ai/core/engine.py#L858-L870) |
| 恶意原文二次注入 | 长期记忆沉淀前先过注入检测，命中直接丢弃 | [engine.py#L427-L438](file:///d:/code/react-ai/src/react_ai/core/engine.py#L427-L438) |
| 历史事实越权 | 长期记忆只作为 `system` 参考块，明确"不是指令" | [engine.py#L547-L560](file:///d:/code/react-ai/src/react_ai/core/engine.py#L547-L560) |

双轨原则：发往 LLM 的文本用脱敏版，业务校验与数据库存储用用户原始输入（需按合规策略另行加密）。

---

## 10. 持久化与恢复

HITL 审批挂起时，上下文必须完整落 checkpoint，而不是只存"最后一条消息"。

`RunCheckpoint` 包含：身份三元组、`messages`、`step_history`、`pending_tool_calls`、`current_step`，见 [core/checkpoint.py#L12-L36](file:///d:/code/react-ai/src/react_ai/core/checkpoint.py#L12-L36)。

企业级要求：

1. **跨进程恢复**：checkpoint 存储必须共享（PG / Redis），`InMemoryCheckpointStore` 仅限单机；
2. **协议完整性**：恢复时为所有未执行的 `tool_calls` 补齐拒绝观测消息；
3. **幂等协同**：恢复执行依赖共享幂等存储，防止已完成的工具副作用被重复执行；
4. **超时清理**：审批超时自动降级为拒绝，避免孤儿 run 永久占用上下文资源。

---

## 11. 可观测性

上下文是"隐形的成本中心"，必须可度量。

### 11.1 Trace Span

`memory.write / read / load / save / delete / clear_session / clear_user` 全部包 span，属性只允许携带 `backend / tenant_id / user_id / session_id / type / 条数 / budget`，**禁止写入消息正文**。

### 11.2 Metrics

| 指标 | 口径 | 用途 |
|---|---|---|
| `memory_ops_total` | op + backend + tenant + result | 调用量与错误率 |
| `memory_op_duration_ms` | op + backend | 后端延迟 |
| `memory_items_total` | 读写/召回条目数 | 验证命中量 |
| `memory_hit_rate` | hit / (hit + miss) | 召回有效性 |
| `memory_evictions_total` | backend | 容量压力 |
| `memory_backend_health` | 最近一次操作结果 | 后端健康度 |

实现见 [observability/metrics.py#L107-L173](file:///d:/code/react-ai/src/react_ai/observability/metrics.py#L107-L173)、聚合见 [L283-L325](file:///d:/code/react-ai/src/react_ai/observability/metrics.py#L283-L325)。

### 11.3 关键告警事件

- `memory_token_budget_truncated`：历史被裁剪，附带 budget / dropped / kept；
- `memory_load_session_failed`：入口召回失败，已降级为空历史；
- `memory_save_turn_failed`：出口回写失败，本轮结果已返回但未持久化；
- `memory_capacity_exceeded`：触发淘汰。

### 11.4 建议告警阈值 ⏳

| 指标 | 建议阈值 | 含义 |
|---|---|---|
| 截断率 | 单租户 > 30% | 历史预算偏小或用户会话过长 |
| 记忆错误率 | error / total > 1% | 后端故障 |
| P95 读取延迟 | > 100ms | 存储层需要扩容或建索引 |
| 长期记忆命中率 | < 20% | 召回过宽或沉淀质量差 |

---

## 12. 配置清单

配置定义见 [config/settings.py#L24-L43](file:///d:/code/react-ai/src/react_ai/config/settings.py#L24-L43)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMORY_TYPE` | `in_memory` | 记忆后端：`in_memory` / `redis` |
| `SESSION_TTL` | 86400 | 会话历史 TTL（秒） |
| `TOOL_RESULT_TTL` | 3600 | 工具结果缓存 TTL（秒） |
| `WORKING_TTL` | 1800 | 工作记忆快照 TTL（秒） |
| `HISTORY_TOKEN_BUDGET` | 4000 | 历史注入 token 预算，`<=0` 不启用 |
| `ENFORCE_TENANT_ISOLATION` | `True` | 租户隔离硬闸 |
| `RunContext.budget_tokens` | 100000 | 单次运行 token 上限 |

---

## 13. 企业级铁律

1. **禁止裸 `session_id`**：所有会话读写必须经 `SessionKey` 三元组。
2. **禁止无租户读写**：`write/read/clear_*` 缺 `tenant_id` 一律拒绝。
3. **预算裁剪至少保留 1 条**：宁可超限，不可失忆。
4. **记忆故障必须降级**：召回/沉淀失败只告警，不阻断推理。
5. **工具中间消息不落会话历史**：避免残缺工具协议回放。
6. **全部 `tool_calls` 聚合单条 assistant 消息**：保证协议合法与上下文顺序稳定。
7. **注入原文不入库**：沉淀前必须过护栏，防止二次注入。
8. **长期记忆只作参考**：注入时必须显式声明"不是指令"。
9. **指标上报容错**：可观测层异常不得影响上下文主流程。
10. **组装顺序确定**：固定前缀，保证 KV Cache 命中与结果可复现。

---

## 14. 现状与演进路线

| 能力 | 现状 | 下一步 |
|---|---|---|
| 分层上下文模型 | ✅ 七层已落地 | 补齐 L1 验收测试（2.3） |
| 多轮会话历史 | ✅ 三元组 + TTL | 历史压缩摘要 ⏳ |
| Token 预算 | ✅ 历史预算 + 运行预算 + Observation 截断 | 按模型精确分词（tiktoken）⏳ |
| 租户隔离 | ✅ 硬闸默认开启 | 审计日志联动 ⏳ |
| 长期记忆 | ✅ 向量读写 + 自动沉淀 | 召回质量评测集 ⏳ |
| 工作记忆 / 工具缓存 | ✅ 已接入引擎 | 冲突与版本策略 ⏳ |
| 可观测 | ✅ span + metrics + 告警事件 | 截断率/命中率看板 ⏳ |
| 分布式恢复 | 🟡 InMemory checkpoint 缺省 | 共享 checkpoint store + 幂等存储 ⏳ |
| 上下文安全 | ✅ 注入 + PII + 工具输出护栏 | 上下文级敏感数据分级 ⏳ |

---

## 15. 验收清单

- [ ] 同一 `session_id` 第二次请求，LLM 能收到首轮对话；
- [ ] 不同 `tenant/user` 下相同 `session_id` 字符串互不可见；
- [ ] 历史超预算时从最旧丢弃，且至少保留最新 1 条；
- [ ] `budget_tokens` 耗尽时返回明确的预算提示并正常 `RUN_END`；
- [ ] 记忆后端异常时主流程仍能完成，且产生告警事件；
- [ ] 注入内容被阻断，且不写入会话历史与长期记忆；
- [ ] HITL 挂起后可从 checkpoint 恢复，未执行工具调用有拒绝观测；
- [ ] `get_stats()` 能看到截断次数、截断条数与隔离开关状态；
- [ ] 指标中能查到 `memory_hit_rate`、错误率与后端健康度。

---

## 相关文档

- [Memory](Memory.md)：分层记忆模块实现细节
- [Memory-Gap-Analysis](Memory-Gap-Analysis.md)：记忆模块企业级差距与落地记录
- [Core Engine](Core-Engine.md)：ReAct 状态机与 RunContext
- [Prompts](Prompts.md)：提示词渲染、版本化与灰度
- [Guardrails](Guardrails.md)：注入拦截与 PII 脱敏
- [Observability](Observability.md)：追踪、日志与指标
