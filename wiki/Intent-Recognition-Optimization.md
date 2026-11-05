# 意图识别优化方案（第二轮）—— 从"合规格"到"可持续迭代"

> 状态：**规划文档**（§2.1 评测集与回归测试已落地 ✅，§2.2 真实链路端到端验证已落地 ✅，§2.3 可观测性与会话串行化已落地 ✅，§3.1 置信度与阈值策略已落地 ✅，其余待实施） · 前置：[Intent-Recognition.md](Intent-Recognition.md) 六项差距 §2.1–§2.6 已全部落地 ✅
> 更新时间：2026-10-31 · 对象：[examples/aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py)

---

## 1. 基线与本文定位

第一轮改造后，意图模块已具备：function calling 结构化抽槽、Pydantic 强校验、槽位业务校验（存在性+归属权+失败回流）、OTHER 越界出口、输入护栏（注入 BLOCK + PII 脱敏）、原生异步、PG 检查点持久化。**这些解决的是"能不能安全上线"。**

第二轮要解决的是另外三个问题：

- **改得动吗**——没有评测集，任何提示词/模型变更都是盲改（P0）
- **扛得住真实会话吗**——置信度、意图切换、历史膨胀是真实用户流量的必然冲击（P1）
- **守得住输出吗**——护栏只守输入，模型生成的话术未经检查直达用户（P2）

---

## 2. P0 基础设施缺口

### 2.1 评测集与回归测试 —— 改进无法度量 ✅ 已实现

> **状态更新（2026-10-29）**：本节已落地。标注数据集 [eval/datasets/intent_slots.jsonl](file:///D:/code/react-ai/eval/datasets/intent_slots.jsonl) 共 114 条，分层为 槽位齐全 20 / 缺失追问 18 / 多轮补槽 16 / 口语错别字 16 / 越界 OTHER 16 / 注入 12（含 3 条假阳性对照）/ PII 12（含 2 条假阳性对照）/ 订单业务校验 4；断言覆盖意图与等价集合、三个关键槽位（`null` 即反幻觉断言）、缺失清单、追问触发（由图自身的 `route_slots` 判定而非猜测）、护栏动作、脱净校验、越权话术。执行器 [eval/run_intent_eval.py](file:///D:/code/react-ai/eval/run_intent_eval.py) 逐条还原状态跑 `intent_recognition_node`，输出意图准确率 / 槽位 micro-F1 / 追问触发正确率 / 注入拦截率 / PII 脱净率 / 反幻觉率 / 用例通过率的**分层得分**，支持 `--mode llm|fallback|auto`（无 `LLM_API_KEY` 自动评测关键词降级下限基线）与 `--db live|stub|auto`（无 PG 时用 `order_fixture.json` 桩数据保证 CI 可复现）；`--baseline`/`--record-baseline` 给出分层 Δ 与逐条 pass→fail 翻转清单，`--fail-under`/`--fail-on-regression` 提供 CI 退出码。用法与指标口径见 [eval/README.md](file:///D:/code/react-ai/eval/README.md)，另有 [tests/unit/test_intent_eval_dataset.py](file:///D:/code/react-ai/tests/unit/test_intent_eval_dataset.py) 在零依赖下守护数据集结构与分层覆盖。
>
> **首轮量化结果**（同数据集对比，即"改进无法度量"的终结）：真实 LLM（deepseek-chat）用例通过率 **93.0%**、意图准确率 99.1%、槽位 F1 97.9%、追问触发 95.6%、注入拦截 66.7%、PII 脱净 100%、耗时 P95 1.9s（全量 114 条约 30 秒，满足 5 分钟内量化）；关键词降级下限基线用例通过率 **80.7%**、意图准确率 87.6%。评测集随即暴露四处真实缺口并已记录在 README §9：注入正则漏"忽略之前**的**指示 / ignore **the** above instructions"变体（当前 6/9）、降级路径 `\b\d{8,}\b` 把手机号当订单号、`reason_type` 对模糊表述过度推断（§3.1 待标定）、关键词路径无法区分仅退款与退货退款。以下为原方案。

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

### 2.2 真实链路端到端验证 ✅ 已实现

> **状态更新（2026-10-29）**：本节已落地，完整数据见 [Intent-Recognition-E2E-Report.md](Intent-Recognition-E2E-Report.md)。前置条件已具备（`.env` 配 `deepseek-chat` key、5433 有 PostgreSQL 17.10 与 `ecommerce_aftersales` 库），首跑前 `checkpoints` 表为 0 行，故为本链路首次真实执行。**三用例**在真实 LLM + `AsyncPostgresSaver` 下全部按预期收敛（`RETURN_SHIPPING` / 追问后 `RETURN_SHIPPING` / `ESCALATE_HUMAN`，落库 26 条检查点），过程中修正了主示例用例 2 的参数与种子数据不匹配（原用 `USR_002` 提交 `USR_001` 的订单，必然触发归属权拒绝，见 [aftersales_langgraph.py:857-870](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L857-L870)）。**重启续跑**达成：进程 A(PID 1284) 挂起待补槽后退出，新进程 B(PID 32512) 仅收到裸订单号 `ORD_CLOTH_001`（无意图词、无原因词）即从检查点恢复出 `RETURN_AND_REFUND`+`quality_issue` 并收敛，`slot_rounds` 跨进程保留。**多实例**顺序交替一致（PID 24740→39844 同会话接管），但**同一 `thread_id` 并发请求会使检查点分叉**——两实例各见自身轮次、生成两条 head，后续轮次只延续其中一条而孤立另一条，且两分支各自完成写操作（一次并发落下 2 条用户通知）；需在 §2.3 同批引入 `thread_id` 级串行化（PG advisory lock 或网关按会话排队）。**抽槽来源**：可观测的 52 次调用 **100% 原生 tool_calls、正文 JSON 回退 0 次**；全量 114 条复现 §2.1 记录分数（通过率 93.0%、意图 99.1%、槽位 F1 97.9%），108 次抽取中仅 1 次被 Pydantic 拒绝（模型输出 `ord cloth 001` 违反订单号格式，剔除后正确回流追问）。挑出 3 条分层用例做端到端全图验证：C001（槽位齐全）、M001（缺失追问+跨进程补槽）、V001（订单越权+跨实例换单），执行器判定用例通过率 100%、反幻觉 100%。驱动脚本 [scripts/live_e2e_verify.py](file:///d:/code/react-ai/scripts/live_e2e_verify.py) 以"单轮"为粒度，旁路观测抽槽来源、不改变图行为。以下为原方案。

**现状**：本机无 `LLM_API_KEY`、5433 无 PG——function calling 强制调用、PG 跨进程重启续跑两条核心路径**只被 mock 证明过接口正确**。

**方案**：
- 准备 `.env`（`LLM_API_KEY` + `DATABASE_URL` 指向真实 PG），跑 `py examples/aftersales_langgraph.py` 三用例 + 评测集全量；
- 重启进程后用原 `thread_id` 发补槽消息，验证检查点续跑；
- 两实例（不同端口）交替打同一会话请求，验证多实例状态一致；
- 记录真实 tool_calls 命中率与正文 JSON 回退比例（验证 §2.1 收益）。

**验收标准**：真实模型 + 真实 PG 下三用例全部按预期决策收敛；重启续跑成功。

### 2.3 可观测性 —— print 无法支撑运维 ✅ 已实现

> **状态更新（2026-10-29）**：本节已落地，六类指标全部可查询、多轮会话轨迹可按 `thread_id` 完整还原，§2.2 追加的会话级串行化同批完成（[Intent-Recognition-E2E-Report.md](Intent-Recognition-E2E-Report.md) §6.2 随之关闭）。
>
> **指标层**：[observability/metrics.py](file:///d:/code/react-ai/src/react_ai/observability/metrics.py) 的 `MetricsCollector` 补齐带标签能力——`inc(name, labels)` / `counter(name, labels)` 为标签计数器，`observe(name, value, labels)` / `histogram(name, labels)` / `histogram_series(name)` 为直方图（`count`/`sum` 全量累计，样本有界 1024 条以保 p50/p95/p99 恒定内存），`to_prometheus_text()` 输出 Prometheus 文本（计数器原样、直方图按 summary 导出分位数），`reset()` 供测试隔离；`record_request`/`record_tool_call`/`snapshot` 旧字段保留，ReAct 主链路与生命周期钩子无需改动。本表六类指标即以上述 API 落地，另有 `checkpoint_fork_total` 反查并发冲突频次。
>
> **日志层**：[observability/logger.py](file:///d:/code/react-ai/src/react_ai/observability/logger.py) 修正为按 `settings.observability.log_level` 取级别（此前固定 INFO 导致 DEBUG/WARNING 语义丢失），输出走 stderr 以保护脚本 stdout 的 JSON 结果，新增 `bind(**fields)` 生成携带关联键的子 logger。节点内全部中文 `print` 替换为单行 JSON 事件：`thread_id_of` / `trace_of` / `log_node`（[aftersales_langgraph.py:214-237](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L214-L237)）让每条日志固定携带 `{thread_id, session_id, user_id}`，`thread_id` 遵循 §2.6 的 `{tenant}:{user}:{session}` 约定、由状态显式字段或用户/会话推导。事件覆盖节点生命周期（`node_completed` 附 `node`/`latency_ms`/意图/决策/缺失槽位/轮次/答复摘要，`node_failed` 走 ERROR 并原样抛出）、抽槽归因（`llm_slots_validated(via=tool_calls|content_json)` / `llm_slots_rejected` / `llm_slots_unstructured` / `llm_extraction_failed`）、护栏与校验（`guardrail_blocked` / `guardrail_masked` / `slot_validation_failed` / `out_of_scope_intent`）、工具与业务（`tool_call_started` / `tool_call_failed` / `risk_assessment` / `action_execute_started`）、会话与检查点（`session_turn_started` / `session_turn_finished` / `checkpoint_verified` / `checkpoint_fork_detected` / `checkpointer_ready` / `checkpointer_degraded`）。安全约束：护栏日志只记录命中的规则标识与动作，PII 原文与未脱敏正文一律不落日志，`final_reply` 截断 60 字。还原一次会话即 `grep '"thread_id": "default:USR_001:SESS_TEST_002"'`，无需再解析散落的中文 print。
>
> **埋点方式**：`instrument_node(name, fn)`（[aftersales_langgraph.py:252-296](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L252-L296)）以装饰器方式为 9 个节点统一产出 `node_latency_seconds{node}` 与 `decision_total{decision}`（从节点返回的 `decision` 字段取，异步/同步节点均支持），业务节点代码不必重复写计时代码；`intent_extraction_total{source}`、`slot_validation_fail_total{reason}`（由 `_validation_failure_reason` 把校验话术归为 `system`/`ownership`/`not_found`）、`guardrail_action_total{action}`、`ask_user_rounds` 分别落在对应节点内。
>
> **§2.2 追加项（会话级串行化）**：`serialize_session(thread_id)` 采用进程内 `asyncio.Lock` 排队 + `pg_advisory_lock(hashtext(thread_id))` 跨进程互持（`SET lock_timeout` 限时，取不到锁即 `session_lock_degraded` 降级并继续服务，绝不因锁失败拒绝用户），标准轮次入口 `ainvoke_serialized(app, inputs, config)` 在串行段结束后用 leaf-checkpoint SQL 统计该线程 head 数，`heads > 1` 则 `checkpoint_fork_total` +1 并打 ERROR 级 `checkpoint_fork_detected`。开关由 `ENABLE_SESSION_LOCK` / `SESSION_LOCK_TIMEOUT`（[settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py)）控制，默认开启。`main_async` 与 [scripts/live_e2e_verify.py](file:///D:/code/react-ai/scripts/live_e2e_verify.py) 全部改走该入口，后者新增 `fork-check --thread <id>` 子命令直接输出 `{heads, linear_history, fork_metric_total}`。
>
> **暴露与验收**：[api/main.py](file:///D:/code/react-ai/api/main.py) 新增 `/metrics`（Prometheus 文本）与 `/metrics/snapshot`（JSON），演示脚本结尾 `format_metrics_report()` 打印同类汇总。真实链路复验：同一新线程 `default:USR_001:E2E_SER_1` 上两个并发进程执行两轮流，结果 `heads=1`、`linear_history=true`、`checkpoint_fork_total=0`，第二轮正确接续第一轮的槽位状态（§6.2 的分叉缺陷消除）；对照旧的分叉线程 `E2E_CONC` 仍报告 `heads=2`，证明检测有效。单元层面新增 [tests/unit/test_observability.py](file:///D:/code/react-ai/tests/unit/test_observability.py)（标签计数、分位数、样本有界、Prometheus 文本与标签转义、`reset`、日志级别与 JSON 结构、`bind`）与 [tests/unit/test_aftersales_observability.py](file:///D:/code/react-ai/tests/unit/test_aftersales_observability.py)（零依赖离线断言六类指标各自增长、每条日志携带 `thread_id`、同线程并发度恒为 1 而不同线程可并行），全量 178 条单测通过；离线评测基线未受影响（用例通过率 80.7%、意图准确率 87.6%，与 §2.1 记录一致）。以下为原方案。

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

**§2.2 追加项**：真实链路验证发现同一 `thread_id` 的并发请求会让检查点分叉（两条 head、后续只延续其一、两分支各自落库），故本节需同时落地**会话级串行化**（`pg_advisory_xact_lock(hashtext(thread_id))` 或网关按会话排队），并以 `checkpoint_fork_total` 指标反查冲突频次——详见 [Intent-Recognition-E2E-Report.md](Intent-Recognition-E2E-Report.md) §6.2。

**验收标准**：一次完整多轮会话可通过 thread_id 在日志系统还原全部节点决策轨迹；六类指标可查询。

---

## 3. P1 能力升级缺口

### 3.1 置信度与阈值策略 ✅ 已实现

> **状态更新（2026-10-31）**：本节已落地，LLM 自评分进入阈值分流，"意图模糊"第一次有了保护动作；阈值取自评测集全量标定而非拍脑袋。以下为实施记录。
>
> **自评分来源（三处同一契约）**：`SLOT_TOOL.parameters` 新增 `confidence`（`type=number, minimum=0, maximum=1`）并列入 `required`——槽位可以缺（由节点统一算缺失清单触发追问），自评分必须给；`SlotExtractionResult` 新增 `confidence: Optional[float] = Field(ge=0.0, le=1.0)`，配 `mode="before"` 归一化器兜住模型常见的口径错误（`"0.95"`→0.95、`90`→0.9、越界值夹回 `[0,1]`、`"看不清"`→视为未自评）；`SLOT_EXTRACTION_PROMPT` 给出评分口径（动作与措辞明确给 0.9 以上，用户只陈述问题或只说"退"给 0.5~0.8，几乎无法判断给 0.5 以下），并要求"助手已反问确认、用户回『是』即按该意图给 0.9 以上"。关键词降级路径同样自评分（`fallback_extract_slots`：单一动作词命中 0.9，"退"与"换"同时命中＝规则在猜＝0.7，OTHER 与弃权不给分），两路输出进同一段阈值代码，不存在"降级就绕过保护"。
>
> **三段动作**（[aftersales_langgraph.py:626-668](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L626-L668)，阈值来自 [settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py) 的 `IntentConfidenceConfig`，默认 `0.8`/`0.5`，`INTENT_CONFIDENCE_HIGH|LOW|ENABLED` 可调可灰度关闭）：`≥0.8` 直接进售后链（`band=high, action=proceed`）；`[0.5,0.8)` 若必要槽位齐全则反问"您是要办理【退货退款】吗？"并挂起流程（`band=mid`），仍缺槽则本轮先补槽、把确认留到槽位齐全那一轮（`band=mid_deferred`，一轮只抛一类问题）；`<0.5` 作废本轮意图与自评分，自然回落到既有的"请问您希望如何处理：退货退款、仅退款，还是换货？"补槽提问（`band=low, action=ask_intent_clarify`）。**这里是与方案文本的唯一有意偏差**：方案写"<0.5 按 OTHER 或转人工"，实现选择不直接开工单——低置信时向用户反问"您要办哪一种"是零成本且不会出错的，直接转人工反而把一次模型不确定兑换成一张工单；真正的兜底仍由既有的 3 轮补槽上限承担（追问耗尽 → `ESCALATE_HUMAN`）。确认反问完全复用 `slot_error → ask_user` 回流通道，图的节点与边零新增，唯一的路由改动是 `route_slots` 增加一条判据：槽位齐全但 `intent_confirm_pending` 非空时不得进 `query_order`（[aftersales_langgraph.py:1035-1042](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1035-L1042)）——否则"待确认"会被当成齐全直接办理，本节保护形同虚设（该缺陷由 [tests/unit/test_intent_confidence.py](file:///d:/code/react-ai/tests/unit/test_intent_confidence.py) 的第一版断言抓出）。
>
> **回流语义**：用户答裸肯定词（`是/对/嗯/好的/没问题`，≤8 字）→ 直接锁定待确认意图为 0.98 并**跳过这一次 LLM 调用**（`band=confirmed`，`source=用户确认`），用户裁决优先于模型自评分；答否定（`不是/不对/不想/错了`，既肯又否按否定处理）→ 作废意图与自评分、交回模型重判；答非所问 → 作废旧确认、按新一轮自评分决定是否再问（不会同时挂两个问题）。自评分是"本轮这次判定"的属性，因此每轮刷新（不像其他槽位 accumulate-never-overwrite），且模型整体未返回自评分的轮次标 `band=unrated` 只观测不拦截——避免"模型停止自评"退化成"每句都反问"。
>
> **标定结果**（`python eval/run_intent_eval.py --mode llm --db stub`，deepseek-chat，114 条，108 条产出自评分）：意图判对的 104 次自评分 均值 0.887 / P10 0.70 / P50 0.95 / P90 0.97，意图判错的那 1~2 次自评分 0.30~0.40——**过度自信是真的（P50 已顶到 0.95），但低分确实与判错同时出现**，所以 0.8 这条线的实际作用不是"过滤大部分"（85/108 落在 ≥0.8），而是把 0.5~0.8 的"措辞只说『退』"这一类明确交给用户裁决；落段分布 `high=69、mid=12、mid_deferred=4、low=5~6、out_of_scope=17~18、guardrail_blocked=6`。据 `--confidence-report` 的切点扫描，误判样本仅 1~2 条，统计上不足以支撑下移 `high`，故维持默认 0.8/0.5 并在报告里显式写明"待评测集扩充错例后复标"。
>
> **分数影响与回归**：阈值拦截开启后 LLM 路径用例通过率 **93.0%（106/114）**，与 §2.1/§2.2 记录的改造前基线持平，追问触发 95.6%、意图准确率 99.1%、槽位 F1 98.6%、高置信断言达标 8/8、置信度落段断言 16/16；关键词降级路径 **80.7%** 与改造前完全一致（自评分 0.9 落在放行段，未引入新失败）。唯一代价是 6 条"只说『退』"的话术（C004/L003/L006/L015/MT005/P003，模型自评 0.75）多花一轮确认——这正是本节想要的摩擦，因此把这 6 条的标注由 `ask_user=false` 改为 `ask_user=true, band=mid`（附 `note` 说明理由），而不是放宽阈值把它们藏回通过率里；两份基线 `eval/baselines/{llm_deepseek_chat,fallback_keywords}.json` 已按"含置信度契约 + 拦截开启"重录。已知残留：O010（"价格能便宜点吗"）模型给出 0.30 的具体意图而非 OTHER，于是走补槽提问而不是越界出口——安全但多一轮，属自评分粒度限制。
>
> **可观测与守护**：新增 `intent_confidence` 直方图（每次抽槽上报自评分，供线上继续标定）、`intent_confidence_total{band,action}`、`intent_confidence_band_total{band}`、`intent_confirmation_total{result=accept|reject|pending}`，节点日志事件 `intent_confidence_gate`（含 `confidence_source=model|inherited|absent` 与当时阈值区间）与 `intent_confirmation`，状态字段 `confidence_band` 进入轨迹字段集，可按 `thread_id` 还原"这一轮为什么放行/为什么反问"。评测集新增断言键 `confidence_min` 与 `band`（8 条高置信直放锚点 + 6 条中置信确认锚点 + 2 条越界落段），[tests/unit/test_intent_eval_dataset.py](file:///d:/code/react-ai/tests/unit/test_intent_eval_dataset.py) 校验其枚举合法性并强制"必须存在 ≥4 条高置信锚点"（否则有人把 `high` 调到 0.99 也不会有测试报警）；行为层面 [tests/unit/test_intent_confidence.py](file:///d:/code/react-ai/tests/unit/test_intent_confidence.py) 20 条离线用例覆盖三段动作、阈值来自配置、`unrated` 不拦截、确认回流的是/否/答非所问、`ask_user` 话术优先级、补槽轮次耗尽升级人工、关键词路径同样受阈值策略约束。全量 200 条单测通过。

**现状**：LLM 说 `RETURN_AND_REFUND` 就无条件执行；"意图模糊"不会触发任何保护（只有槽位缺失会）。

**方案**：
- `SlotExtractionResult` 与 `SLOT_TOOL` Schema 增加 `confidence: float (0~1)`，提示词要求模型自评；
- 阈值策略：`≥0.8` 直接执行 / `0.5~0.8` 反问确认意图（"您是想办理退货退款吗？"）/ `<0.5` 按 OTHER 或转人工；
- 确认话术复用现有 `slot_error` 回流机制，不新增图结构。

**风险**：LLM 自评分数普遍过度自信，阈值需靠 2.1 评测集标定，不能拍脑袋。**标定结论（2026-10-31）**：P50 确实顶到 0.95（过度自信成立），但判错样本的自评分同时落在 0.30~0.40，说明低分有真实信号；当前标注集只有 1~2 条错例，不足以移动阈值，维持 0.8/0.5 并把"扩充错例后复标"写进 `--confidence-report` 输出。

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
| **第一波** | 2.1 评测集+回归脚本 ✅ → 2.2 真实链路验证 ✅ | 没有度量与真实环境，后续一切改动无从验证、无从回归 |
| **第二波** | 2.3 可观测性 ✅ + 会话串行化 ✅ → 4.1 输出护栏 → 3.3 历史窗口化 | 上线运维与合规刚需，改造量小 |
| **第三波** | 3.1 置信度阈值 ✅ → 4.2 缓存/分级 | 依赖第一波的评测集标定阈值 |
| **第四波** | 3.2 换意图挂起 → 3.4 意图体系解耦 | 状态机/架构级改动，必须全程有回归保护 |

---

## 6. 相关文件索引

- 评测集与回归（§2.1 已落地）：[eval/README.md](file:///D:/code/react-ai/eval/README.md) · 数据集 [eval/datasets/intent_slots.jsonl](file:///D:/code/react-ai/eval/datasets/intent_slots.jsonl) · 执行器 [eval/run_intent_eval.py](file:///D:/code/react-ai/eval/run_intent_eval.py) · 基线 [eval/baselines/](file:///D:/code/react-ai/eval/baselines)
- 真实链路验证（§2.2 已落地）：[Intent-Recognition-E2E-Report.md](Intent-Recognition-E2E-Report.md) · 单轮驱动脚本 [scripts/live_e2e_verify.py](file:///D:/code/react-ai/scripts/live_e2e_verify.py)
- 可观测性与会话串行化（§2.3 已落地）：指标与日志组件 [observability/metrics.py](file:///D:/code/react-ai/src/react_ai/observability/metrics.py)、[observability/logger.py](file:///D:/code/react-ai/src/react_ai/observability/logger.py) · 埋点与锁实现 [aftersales_langgraph.py](file:///D:/code/react-ai/examples/aftersales_langgraph.py)（`instrument_node` / `serialize_session` / `ainvoke_serialized` / `format_metrics_report`）· 抓取端点 [api/main.py](file:///D:/code/react-ai/api/main.py)（`/metrics`、`/metrics/snapshot`）· 分叉自检 `python scripts/live_e2e_verify.py fork-check --thread <thread_id>` · 守护测试 [tests/unit/test_observability.py](file:///D:/code/react-ai/tests/unit/test_observability.py)、[tests/unit/test_aftersales_observability.py](file:///D:/code/react-ai/tests/unit/test_aftersales_observability.py)
- 置信度与阈值策略（§3.1 已落地）：阈值配置 [settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py)（`IntentConfidenceConfig` / `INTENT_CONFIDENCE_HIGH|LOW|ENABLED`）· 闸门实现 [aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py)（`_confidence_band` / `intent_recognition_node` 的置信度段 / `route_slots` 待确认判据 / `fallback_extract_slots` 规则自评）· 标定执行器 `python eval/run_intent_eval.py --confidence-report eval/reports/confidence-llm.md`（另有 `--confidence-high/--confidence-low/--no-confidence-gate`）· 守护测试 [tests/unit/test_intent_confidence.py](file:///d:/code/react-ai/tests/unit/test_intent_confidence.py)
- 第一轮差距与实施记录：[Intent-Recognition.md](Intent-Recognition.md)
- 当前实现：[aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py)（`extract_slots_via_llm` / `SlotExtractionResult` / `validate_order_slot` / `open_checkpointer`）
- 可观测组件：[Observability.md](Observability.md) · 记忆窗口组件：[Memory.md](Memory.md) · 意图路由：[Orchestration.md](Orchestration.md)
- 输出护栏可用组件：[guardrails/moderation.py](file:///d:/code/react-ai/src/react_ai/guardrails/moderation.py)、[guardrails/base.py](file:///d:/code/react-ai/src/react_ai/guardrails/base.py)（`direction` 过滤）
