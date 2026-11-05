# HITL 审批与检查点模块企业级差距分析

> 适用对象：[src/react_ai/approval/](file:///d:/code/react-ai/src/react_ai/approval/) 与 [core/checkpoint.py](file:///d:/code/react-ai/src/react_ai/core/checkpoint.py)，及其在引擎挂起/恢复链路中的实际表现。
> 本文档回答：**当前审批+检查点到了什么水准？距离企业级生产人工介入（HITL）体系还差什么、怎么补？**
> 更新时间：2026-11-04（已按代码现状核对各项落地状态）

---

## 1. 当前架构速览

```text
 工具沙箱                审批服务                    核心引擎
┌────────────┐ evaluate_gate ┌──────────────────┐   ┌─────────────────────┐
│ sandbox.py │──────────────►│ ApprovalService   │   │ 挂起: save checkpoint│
│ L82-L109   │  抛错挂起      │ InMemoryStore ⚠️  │◄──│ _save_checkpoint    │
└────────────┘               │ AuditChain(内存)⚠️│   │ engine.py L367-L382 │
                             └──────────────────┘   └─────────────────────┘
        ▲                              │                      ▲
        │  /approvals/{id}/decision    │ decide()             │ /runs/{id}/resume
        │  ⚠️ 匿名可调（见 API 2.1）     │ 懒超时→EXPIRED        │ agent.py L110-L120
        └──────────────────────────────┴──────────────────────┘
   进程内存单例存储：重启即丢、多副本互不可见、无 TTL、无通知渠道
```

### 已实现能力清单

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 审批单模型 | [approval/models.py#L10-L42](file:///d:/code/react-ai/src/react_ai/approval/models.py#L10-L42) | 状态枚举（PENDING/APPROVED/REJECTED/EXPIRED/CONSUMED）、`expires_at` 超时判定 |
| 审批闸门 | [approval/service.py#L15-L74](file:///d:/code/react-ai/src/react_ai/approval/service.py#L15-L74) `evaluate_gate` | 创建审批单、设超时、落存储、记审计、抛 `ApprovalRequiredError` 挂起 |
| 决策处理 | [approval/service.py#L78-L138](file:///d:/code/react-ai/src/react_ai/approval/service.py#L78-L138) `decide` | 懒超时降级 EXPIRED、重复决策保护、审批/拒绝均记审计 |
| 防篡改审计链 | [approval/store.py#L49-L98](file:///d:/code/react-ai/src/react_ai/approval/store.py#L49-L98) `AuditChain` | append-only 哈希链，`verify()` 篡改检测 |
| 挂起保存检查点 | [core/engine.py#L367-L382](file:///d:/code/react-ai/src/react_ai/core/engine.py#L367-L382) | 保存剩余 tool_calls，发 `TOOL_APPROVAL_REQUIRED`/`RUN_PAUSED` 事件 |
| 断点续跑 | [core/engine.py#L122-L199](file:///d:/code/react-ai/src/react_ai/core/engine.py#L122-L199) `resume` | 恢复 state、按审批结果执行/拒绝/保持挂起、完成删检查点 |
| 超时配置 | [config/settings.py#L51](file:///d:/code/react-ai/src/react_ai/config/settings.py#L51) `APPROVAL_TIMEOUT_SECONDS` | 默认 3600s，经 [service.py#L23-L26](file:///d:/code/react-ai/src/react_ai/approval/service.py#L23-L26) 真实生效 |
| API 端点 | [api/routes/approvals.py#L19-L60](file:///d:/code/react-ai/api/routes/approvals.py#L19-L60) · [agent.py#L110-L120](file:///d:/code/react-ai/api/routes/agent.py#L110-L120) | 列表（含审计链校验）、决策、SSE 恢复 |
| 端到端测试 | [tests/unit/test_hitl_approval.py#L131-L243](file:///d:/code/react-ai/tests/unit/test_hitl_approval.py#L131-L243) | 超时降级、重复决策、篡改检测、approve/reject/pending 三路径 resume |

**结论：审批状态机、审计链、挂起/恢复语义是本项目完成度最高的企业级设计之一。但存储全部是进程内存单例，审批人身份自报且无鉴权，挂起后"无人知晓、无人认领、永久悬挂"——生产多副本部署下整套 HITL 会静默失效。存在下述 8 项差距。**

---

## 2. 企业级差距分析（按重要性排序）

### 2.1 进程内存态存储 —— 重启即丢、跨副本不可见 📦 P0 ❌ 未开始

**现状**：审批与检查点都只有 `InMemory` 实现——[approval/store.py#L10-L46](file:///d:/code/react-ai/src/react_ai/approval/store.py#L10-L46) `InMemoryApprovalStore`、[core/checkpoint.py#L38-L60](file:///d:/code/react-ai/src/react_ai/core/checkpoint.py#L38-L60) `InMemoryCheckpointStore` + 进程级单例 `get_checkpoint_store()`。抽象接口注释写着"生产环境可替换为 Redis/PostgreSQL 实现"（[store.py#L11](file:///d:/code/react-ai/src/react_ai/approval/store.py#L11)），但不存在任何第二实现，也没有配置装配工厂。

**风险**：
1. 服务重启/发布 → 所有 PENDING 审批单与挂起运行凭空消失，业务侧退款等挂起操作**永久悬挂且不可恢复**；
2. 多副本部署：审批请求打到 B 副本、挂起发生在 A 副本 → `/approvals` 列表不全、`/resume` 必然 404；
3. 与 Prompts 2.10 同类缺口，但后果更重——丢的不是配置是**业务事务中间态**。

**企业级做法**：实现 `PostgresApprovalStore` / `PostgresCheckpointStore`（复用 `DatabaseConfig`，表结构含 run_id 主键、status、pending_tool_calls JSONB、expires_at 索引），仿照 `build_audit_store_from_settings` 建工厂按配置装配；检查点序列化沿用 `RunCheckpoint` 现有字段。

**验收标准**：双 worker 部署下 A 挂起 B 可查可恢复；重启后 PENDING 审批单与检查点完整存活；配置缺省仍走内存实现不阻断本机测试。

---

### 2.2 审批人身份自报、无权限分离 🔒 P0 ❌ 未开始

**现状**：决策端点（[api/routes/approvals.py#L50-L65](file:///d:/code/react-ai/api/routes/approvals.py#L50-L65)）无鉴权（API-Service-Gap-Analysis 2.1），`decided_by` 来自请求体自报字段，服务端不校验身份、不校验该身份是否具备审批权限，也不校验**申请人 ≠ 审批人**。

**风险**：触发高危工具的那次对话（乃至任何知道 approval_id 的人）可以自行批准——HITL 的"Human"可以是攻击脚本，四眼原则为零；审计链虽防篡改，但记录的是伪造身份。

**企业级做法**：
1. `decided_by` 从认证主体（JWT/API Key 映射的用户）取值，禁止请求体传入；
2. 审批权限模型：审批单绑定 `required_role`（按工具风险等级），决策时校验主体角色；
3. 强制 `requester_id != approver_id`；
4. 身份写入 `AuditChain` 记录。

**验收标准**：无权限主体决策返回 403；自批自被拒并有审计；`decided_by` 无法伪造。

---

### 2.3 挂起后零通知 —— 审批单无人知晓 📢 P1 ❌ 未开始

**现状**：审批挂起只产生 SSE 事件（[engine.py#L367-L382](file:///d:/code/react-ai/src/react_ai/core/engine.py#L367-L382)），客户端断线即失联；无任何带外通知（IM/邮件/webhook）；超时是**懒判定**——只在有人查询/决策时才降级 EXPIRED（[service.py#L78-L138](file:///d:/code/react-ai/src/react_ai/approval/service.py#L78-L138)），无后台扫描。grep 全项目无 `notify`/`webhook`/`escalate`。

**风险**：真实企业场景是"审批人不会挂着 SSE 等单"——没有通知就没有人决策，超时后运行静默 EXPIRED，业务方只看到"退款没发生"，事故归因极难。

**企业级做法**：
1. 审批创建时推送通知渠道（webhook 起步，预留 IM/邮件适配器接口），消息含审批链接与摘要；
2. 后台周期任务扫描过期单：临期提醒、到期降级并通知发起方与运行 owner；
3. 挂起运行提供"待办列表"查询（现有 `/approvals` 已具雏形，需持久化支撑）。

**验收标准**：创建审批单触发一次通知投递（可 mock 验证）；超时降级有主动回调而非仅懒判定；断线客户端重新上线仍能通过待办列表发现单据。

---

### 2.4 超时只有"过期拒绝"，无升级策略 ⏫ P1 ❌ 未开始

**现状**：超时路径唯一——EXPIRED 后 resume 按拒绝处理（[engine.py#L172-L189](file:///d:/code/react-ai/src/react_ai/core/engine.py#L172-L189)）。无提醒重发、无升级给上级审批人、无按工具风险配置"超时自动批准"（低风险）/"超时永久阻塞"（极高风险）策略。

**风险**：企业审批 SLA 的核心是"不遗漏"——过期即拒绝会把可恢复业务变成需要人工重发的故障；对极高风险操作，过期自动放行/拒绝都不该是默认。

**企业级做法**：审批单增加 `escalation_policy`（提醒间隔、升级对象链、终态动作），由 2.3 的后台扫描驱动；策略按工具 `risk_level` 声明式配置。

**验收标准**：临期产生提醒事件；超时按策略升级或进入配置指定终态；不同风险工具可配不同策略。

---

### 2.5 resume 无并发认领保护 🔁 P1 ❌ 未开始

**现状**：[engine.py#L122-L199](file:///d:/code/react-ai/src/react_ai/core/engine.py#L122-L199) `resume()` 先 `checkpoints.load(run_id)`（L124），执行完剩余工具调用后才 `delete`（L199），load→delete 之间无原子认领/锁。两个并发 `/resume` 同一 run_id 会各自加载同一检查点、各自执行 `_run_tool_calls`（L193）。唯一兜底是工具层幂等守卫，而它可被 `ENABLE_IDEMPOTENCY=false` 关闭。

**风险**：重复创建退款单/重复通知客户——正是 HITL 要防的事故在被审批后的执行环节复活；2.1 落 PG 后此窗口更宽（跨副本并发）。

**企业级做法**：检查点认领原子化：内存实现用 `pop(run_id)` 语义（load 即认领）；PG 实现用 `UPDATE ... SET status='RESUMING', claimed_by=?, claimed_at=now() WHERE run_id=? AND status='PENDING'` 条件更新，认领失败返回 409；`claimed_at` 超时可回收。

**验收标准**：并发双 resume 仅一个执行工具调用，另一个得到 409/已认领信号；崩溃节点的认领可被超时回收。

---

### 2.6 检查点无 TTL 与清理 —— 内存泄漏面 🗑 P2 ❌ 未开始

**现状**：挂起运行若永不决策（用户放弃），checkpoint 与其携带的完整 `messages/step_history` 永久驻留内存（[checkpoint.py#L40-L49](file:///d:/code/react-ai/src/react_ai/core/checkpoint.py#L40-L49)），无过期清理、无容量上限。

**企业级做法**：检查点随审批单生命周期过期（审批 EXPIRED 且运行终态后删除）；PG 实现加 `expires_at` 索引 + 周期清理任务；设置挂起运行总量上限。

**验收标准**：过期审批关联的检查点在一个清理周期内消失；压测挂起 N 千运行不导致内存线性增长。

---

### 2.7 审计链不持久化 🔗 P2 ❌ 未开始

**现状**：`AuditChain`（[approval/store.py#L49-L98](file:///d:/code/react-ai/src/react_ai/approval/store.py#L49-L98)）哈希链设计正确，但账本在内存——重启后链从头开始，历史审批轨迹不可追溯，防篡改价值归零。

**企业级做法**：链随审批存储同库落盘（append-only 表，`prev_hash` 列），启动时加载续链；周期性将链头哈希锚定到外部（对象存储/日志），实现"存储管理员也无法整链重写"。

**验收标准**：重启后 `verify()` 仍可校验全部历史；导出某段记录被篡改时校验失败并定位断点。

---

### 2.8 无多级/批量审批流程 🧭 P2 ❌ 未开始

**现状**：审批是单级单签（一个工具调用一张单）。企业常见诉求：高额退款需"主管+财务"双签、同类多单批量批准、审批委托（休假转交）均不支持。

**企业级做法**：审批单升级为流程实例（`steps: [角色, 状态]` 链），全部步骤通过才放行；`/approvals` 支持按条件批量决策（每步仍记录独立审计）。

**验收标准**：双签流程中途拒绝则整单终态；批量决策每单审计完整。

---

## 3. 实施优先级路线

| 级别 | 事项 | 排序依据 |
|---|---|---|
| **P0（阻塞上线）** | 2.1 PG 持久化 → 2.2 审批人身份与权限 | 丢业务中间态与自批自是 HITL 的存在意义问题 |
| **P1（运营闭环）** | 2.3 通知与扫描 → 2.4 升级策略 → 2.5 resume 原子认领 | 无人认领的审批等于没有审批；并发重复执行是资损面 |
| **P2（治理完善）** | 2.6 检查点 TTL → 2.7 审计链落盘 → 2.8 多级/批量 | 长期运行稳定性与流程治理，依赖 2.1 的持久化底座 |

> **落地核对（2026-11-04）**：8 项全部未开始。
> 依赖关系：**2.1 是 2.3/2.4/2.5/2.6/2.7 的公共前置**（后台扫描、认领锁、链落盘都需要共享存储）；2.2 与 API-Service-Gap-Analysis 2.1/2.4（认证与租户链路）同批实施。审批与检查点建议复用同一 PG 库（与提示词审计存储、Memory 长期记忆同库，参见 Prompts-Gap-Analysis §3 配套建设）。

---

## 4. 相关代码与文档索引

- 审批模块：[approval/models.py](file:///d:/code/react-ai/src/react_ai/approval/models.py) · [approval/store.py](file:///d:/code/react-ai/src/react_ai/approval/store.py) · [approval/service.py](file:///d:/code/react-ai/src/react_ai/approval/service.py)
- 检查点：[core/checkpoint.py](file:///d:/code/react-ai/src/react_ai/core/checkpoint.py)
- 引擎接入点：[core/engine.py#L122-L199](file:///d:/code/react-ai/src/react_ai/core/engine.py#L122-L199) `resume` · [core/engine.py#L367-L382](file:///d:/code/react-ai/src/react_ai/core/engine.py#L367-L382) 挂起保存 · [tools/sandbox.py#L82-L109](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py#L82-L109) 闸门
- API：[api/routes/approvals.py](file:///d:/code/react-ai/api/routes/approvals.py) · [api/routes/agent.py#L110-L120](file:///d:/code/react-ai/api/routes/agent.py#L110-L120)
- 幂等兜底：[idempotency/guard.py](file:///d:/code/react-ai/src/react_ai/idempotency/guard.py)
- 测试：[tests/unit/test_hitl_approval.py](file:///d:/code/react-ai/tests/unit/test_hitl_approval.py)
- 参照文档：[Prompts-Gap-Analysis.md](file:///d:/code/react-ai/wiki/Prompts-Gap-Analysis.md)（2.10 外部存储同构） · [API-Service-Gap-Analysis.md](file:///d:/code/react-ai/wiki/API-Service-Gap-Analysis.md)（2.1 匿名审批 / 2.4 租户） · [Guardrails.md](file:///d:/code/react-ai/wiki/Guardrails.md)
