# 意图识别优化方案（第二轮）—— 从"合规格"到"可持续迭代"

> 状态：**规划文档**（未实施） · 前置：[Intent-Recognition.md](Intent-Recognition.md) 六项差距 §2.1–§2.6 已全部落地 ✅
> 更新时间：2026-10-29 · 对象：[examples/aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py)

---

## 1. 基线与本文定位

第一轮改造后，意图模块已具备：function calling 结构化抽槽、Pydantic 强校验、槽位业务校验（存在性+归属权+失败回流）、OTHER 越界出口、输入护栏（注入 BLOCK + PII 脱敏）、原生异步、PG 检查点持久化。**这些解决的是"能不能安全上线"。**

第二轮要解决的是另外三个问题：

- **改得动吗**——没有评测集，任何提示词/模型变更都是盲改（P0）
- **扛得住真实会话吗**——置信度、意图切换、历史膨胀是真实用户流量的必然冲击（P1）
- **守得住输出吗**——护栏只守输入，模型生成的话术未经检查直达用户（P2）

---

## 2. P0 基础设施缺口

### 2.1 评测集与回归测试 —— 改进无法度量

**现状**：零标注数据。第一轮 6 项改造全部靠 mock 单测验证"不崩"，无法回答"抽槽准确率到底多少、换模型后涨还是跌"。

**方案**：

1. 标注文件 `eval/datasets/intent_slots.jsonl`，每行一条用例：

```json
{"id": "E001", "utterance": "我买的衣服破了，要退货退款，订单号是 ORD_CLOTH_001",
 "context": [],
 "expect": {"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_001",
            "reason_type": "quality_issue", "missing_slots": []}}
{"id": "E017", "utterance": "想退掉",
 "expect": {"intent": null, "missing_slots": ["intent?", "order_id", "reason_type"]}}
```

2. 覆盖分层（≥100 条起步）：槽位齐全直答 / 部分缺失触发追问 / 多轮补槽（带 context）/ 错别字与口语（"货到了烂的""不想要了咋退"）/ 越界 OTHER / 注入样本（应 BLOCK）/ 含 PII 样本（应脱敏后抽取）。
3. 回归脚本 `eval/run_intent_eval.py`：逐条构造 State 跑 `intent_recognition_node`，对比 expect 输出**意图准确率、槽位 F1、追问触发正确率、护栏拦截率**；无 `LLM_API_KEY` 时自动评测降级关键词路径（下限基线），有 key 时评测真实 LLM 路径。
4. 接入 CI：提示词/Schema/模型配置变更的 PR 必须附带评测分数 diff。

**验收标准**：一条命令跑出分层得分；改一版提示词能在 5 分钟内量化"哪类用例退步了"。

### 2.2 真实链路端到端验证

**现状**：本机无 `LLM_API_KEY`、5433 无 PG——function calling 强制调用、PG 跨进程重启续跑两条核心路径**只被 mock 证明过接口正确**。

**方案**：
- 准备 `.env`（`LLM_API_KEY` + `DATABASE_URL` 指向真实 PG），跑 `py examples/aftersales_langgraph.py` 三用例 + 评测集全量；
- 重启进程后用原 `thread_id` 发补槽消息，验证检查点续跑；
- 两实例（不同端口）交替打同一会话请求，验证多实例状态一致；
- 记录真实 tool_calls 命中率与正文 JSON 回退比例（验证 §2.1 收益）。

**验收标准**：真实模型 + 真实 PG 下三用例全部按预期决策收敛；重启续跑成功。

### 2.3 可观测性 —— print 无法支撑运维

**现状**：节点内 `print` 中文轨迹，无结构化、无指标、无 trace。上线后无法回答"今天意图识别降级了多少次"。

**方案**：接入项目 [Observability](Observability.md) 模块（结构化日志 + metrics），最小指标集：

| 指标 | 类型 | 用途 |
|---|---|---|
| `intent_extraction_total{source=llm\|fallback}` | counter | 降级触发率——LLM 不可用/输出非法的早期信号 |
| `slot_validation_fail_total{reason=not_found\|ownership\|system}` | counter | 用户填错率与越权尝试检测 |
| `guardrail_action_total{action=block\|modify}` | counter | 注入攻击频次 |
| `ask_user_rounds` | histogram | 补槽轮次分布，>2 轮占比高说明追问话术有问题 |
| `node_latency_seconds{node}` | histogram | 慢节点定位（LLM 节点 vs 工具节点） |
| `decision_total{decision}` | counter | 自动通过/转人工/拒绝比例，成本与体验核心看板 |

每次会话以 `thread_id` 作为 trace 关联键，节点日志携带该键即可全链路串联。

**验收标准**：一次完整多轮会话可通过 thread_id 在日志系统还原全部节点决策轨迹；六类指标可查询。

---

## 3. P1 能力升级缺口

### 3.1 置信度与阈值策略

**现状**：LLM 说 `RETURN_AND_REFUND` 就无条件执行；"意图模糊"不会触发任何保护（只有槽位缺失会）。

**方案**：
- `SlotExtractionResult` 与 `SLOT_TOOL` Schema 增加 `confidence: float (0~1)`，提示词要求模型自评；
- 阈值策略：`≥0.8` 直接执行 / `0.5~0.8` 反问确认意图（"您是想办理退货退款吗？"）/ `<0.5` 按 OTHER 或转人工；
- 确认话术复用现有 `slot_error` 回流机制，不新增图结构。

**风险**：LLM 自评分数普遍过度自信，阈值需靠 2.1 评测集标定，不能拍脑袋。

### 3.2 中途换意图与流程挂起

**现状**：补槽补到一半，用户突然问"帮我查下另一个订单的物流"——新消息被硬塞进旧退货流程的抽取提示词，结果不可预期。

**方案**：
- 意图节点先做**会话状态检测**：存在进行中的流程（`missing_slots` 非空）时，抽取提示词额外要求判断"本轮是补充旧流程信息，还是开启新意图"；
- 新意图 → 旧流程状态快照存入检查点独立命名空间（LangGraph 可用子图或 `pending_flows` 状态字段），开启新流程；
- 旧流程恢复：用户表达"继续退货"时从快照恢复槽位，不必重新补。

**定位**：这是状态机层面的扩展，复杂度最高，建议放在评测集就绪之后（改动需要回归保护）。

### 3.3 对话历史窗口化

**现状**：`dialog_history` 每轮全量追加并整体进提示词——10 轮后 token 成本与干扰信息同步膨胀。

**方案**：项目已有现成组件——[Memory](Memory.md) 的滑动窗口短期记忆（`SlidingWindowMemory`），意图节点取最近 N 轮（建议 6）+ 更早轮次的单行摘要（可选用 LLM 摘要或截断）。3 轮补槽上限内窗口化即可满足，属于低成本对齐项目规范的改造。

### 3.4 意图体系扩展与路由解耦（长期）

**现状**：3+1 意图与抽槽耦合在一次 LLM 调用里。企业级售后真实意图体系 ≥15 类（查物流/催发货/开发票/投诉/价保/咨询…），全塞进一个提示词会让每类都变差。

**方案**：两层架构——前置轻量分类（小模型/嵌入检索，低延迟低成本）判定大类 → `IntentRoute`（[orchestration/router.py](file:///d:/code/react-ai/src/react_ai/orchestration/router.py)）分发到各业务子图，售后子图保留现在的"抽槽+校验"设计。本示例的 `SLOT_TOOL` 退化为售后域内的细粒度抽槽器。

**前置依赖**：必须先有 2.1 评测集——分类器好坏只有标注数据能量化。

---

## 4. P2 安全与成本补强

### 4.1 输出侧护栏

**现状**：`slot_guard_chain` 只守 INPUT。`ask_user` 追问话术、`final_reply` 答复（含政策条款、金额承诺）未经任何检查直发用户。

**方案**：
- `send_notification_node` 与 `ask_user_node` 出口接 OUTPUT 方向链：`ModerationGuardrail`（敏感词，构造时传业务 blocklist）+ 承诺性话术规则（正则检测"保证退款""一定赔付"类越权承诺，MODIFY 为中性表述或 BLOCK 转人工）；
- 项目护栏基类原生支持 `direction=OUTPUT` 过滤，无需新组件。

### 4.2 成本与限流

| 项 | 方案 |
|---|---|
| 重复抽槽缓存 | `hash(dialog_history尾部3轮) → 抽取结果`，短 TTL；补槽场景用户重发同一句可省一次 LLM 调用 |
| 模型分级 | 抽槽固定用小模型（如 deepseek-chat），复杂兜底才升级；`LLMRouter` 主备结构已支持按任务组路由 |
| 租户配额 | 按 `tenant_id` 限 LLM 调用 QPS/日额，超限走关键词降级而非拒绝服务 |

---

## 5. 实施优先级路线

| 波次 | 事项 | 排序依据 |
|---|---|---|
| **第一波** | 2.1 评测集+回归脚本 → 2.2 真实链路验证 | 没有度量与真实环境，后续一切改动无从验证、无从回归 |
| **第二波** | 2.3 可观测性 → 4.1 输出护栏 → 3.3 历史窗口化 | 上线运维与合规刚需，改造量小 |
| **第三波** | 3.1 置信度阈值 → 4.2 缓存/分级 | 依赖第一波的评测集标定阈值 |
| **第四波** | 3.2 换意图挂起 → 3.4 意图体系解耦 | 状态机/架构级改动，必须全程有回归保护 |

---

## 6. 相关文件索引

- 第一轮差距与实施记录：[Intent-Recognition.md](Intent-Recognition.md)
- 当前实现：[aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py)（`extract_slots_via_llm` / `SlotExtractionResult` / `validate_order_slot` / `open_checkpointer`）
- 可观测组件：[Observability.md](Observability.md) · 记忆窗口组件：[Memory.md](Memory.md) · 意图路由：[Orchestration.md](Orchestration.md)
- 输出护栏可用组件：[guardrails/moderation.py](file:///d:/code/react-ai/src/react_ai/guardrails/moderation.py)、[guardrails/base.py](file:///d:/code/react-ai/src/react_ai/guardrails/base.py)（`direction` 过滤）
