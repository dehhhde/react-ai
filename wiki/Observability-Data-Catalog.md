# 可观测性数据清单（Observability Data Catalog）

> 状态：**随 §2.3 落地生成** · 覆盖：结构化日志 / 指标 / 抽槽遥测 / 检查点 · 更新时间：2026-10-29
> 口径来源：代码实测，非设计意图。方案见 [Intent-Recognition-Optimization.md](Intent-Recognition-Optimization.md) §2.3，组件文档见 [Observability.md](Observability.md)。

---

## 1. 数据记录到哪里

| 数据类别 | 落地介质 | 具体位置 | 生命周期 | 如何取数 |
|---|---|---|---|---|
| 结构化日志（售后图全链路） | 进程 **stderr**（`logging.StreamHandler`，单行 JSON） | [logger.py:30](file:///d:/code/react-ai/src/react_ai/observability/logger.py#L30)；logger 名固定 `react-ai` | 进程存活期；**代码内不落文件、不发网络** | 容器/主机采集器（Filebeat、Promtail、Fluent Bit）抓 stderr → ELK/Loki；本地复现 `py examples/aftersales_langgraph.py 2> logs/run.jsonl` |
| 框架与引擎日志（`react_ai.agents.base` 等 stdlib logger） | 同一 stderr handler（子 logger 沿 `react_ai.*` 层级冒泡到 `react-ai`） | 各模块 `logging.getLogger(__name__)` | 同上 | 同上；**注意：这批是纯文本行、非 JSON**，字段检索只能覆盖结构化事件流 |
| 指标（六类 + `checkpoint_fork_total`） | **进程内存**（`MetricsCollector` 单例 + `threading.Lock`） | [metrics.py:33](file:///d:/code/react-ai/src/react_ai/observability/metrics.py#L33) | **进程重启即清零**；计数器按标签序列无界增长，直方图样本仅保留最近 1024 条 | `GET /metrics`（Prometheus 文本，[main.py:30](file:///d:/code/react-ai/api/main.py#L30)）、`GET /metrics/snapshot`（JSON，[main.py:36](file:///d:/code/react-ai/api/main.py#L36)）、演示结尾 stdout 报告（[aftersales_langgraph.py:1076](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1076)） |
| 抽槽来源遥测（tool_calls / content_json / 降级归因） | **JSONL 文件**（旁路，仅验证驱动产出） | [live_e2e_verify.py](file:///d:/code/react-ai/scripts/live_e2e_verify.py) `turn --stats <path>`；字段 `{label, thread_id, pid, source, latency_ms, has_tool_calls, slots}` | 由调用方保存，脚本追加写 | `--stats stats.jsonl` 后按行 jq；未传 `--stats` 时打 `[STATS]` 行到 stdout |
| 会话状态与检查点（分叉探针的数据源） | **PostgreSQL** 表 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | `AsyncPostgresSaver`（[aftersales_langgraph.py:946](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L946)），键 `thread_id` | 持久，按线程累积 | leaf-checkpoint SQL `_HEADS_SQL`（[aftersales_langgraph.py:934](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L934)）；命令行 `py scripts/live_e2e_verify.py fork-check --thread <thread_id>` |
| 业务写结果（通知/工单，非观测数据但常被当证据） | PostgreSQL `notifications` / `after_sales_tickets` | 工具层写库 | 持久 | 直接 SQL；§6.2 分叉曾以此发现重复通知 |
| 离线质量指标（评测分数） | 仓库文件 `eval/baselines/*.json` | [eval/run_intent_eval.py](file:///d:/code/react-ai/eval/run_intent_eval.py) `--record-baseline` | 随版本提交 | `--baseline` 出分层 Δ 与 pass→fail 翻转清单 |

**明确未落地**：OTLP/trace 后端（`AgentTracer.start_span` 为骨架、**当前无任何调用点**，实际不产生 span 数据；跨轮关联完全依赖日志里的 `thread_id`）、指标持久化存储与 Push Gateway、告警规则、日志文件 sink 与采样。`OTEL_EXPORTER_OTLP_ENDPOINT` 配置项存在但未接通。

---

## 2. 观测哪些指标

| 指标 | 类型 | 标签（取值） | 采集时机 | 一次采集点 | 回答什么问题 |
|---|---|---|---|---|---|
| `intent_extraction_total` | counter | `source=llm\|fallback` | 意图节点每轮抽槽**定源后**记一次（LLM 成功且过 Pydantic 校验为 `llm`，含 key 缺失/调用异常/Schema 拒绝/无结构四类归因为 `fallback`） | [aftersales_langgraph.py:458](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L458) | 降级触发率：`fallback / total` 抬升即 LLM 不可用或输出非法的早期信号 |
| `intent_confidence` | histogram | 无 | 意图节点每次拿到自评分（模型自评、往轮沿用或规则打分）时 `observe` 原值，含 `--no-confidence-gate` 关闭拦截的场景 | [aftersales_langgraph.py:620](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L620) | 阈值标定：P50 长期 ≥0.95 说明模型过度自信；调 `INTENT_CONFIDENCE_HIGH/LOW` 前先看这条分布 |
| `intent_confidence_band_total` | counter | `band=high\|mid\|mid_deferred\|low\|confirmed\|unrated\|out_of_scope` | 分段落定时 +1：三段判定（[640](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L640)）、用户当场确认（[556](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L556)）、OTHER 越界短路（[596](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L596)）、有意图却无自评分（[626](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L626)） | 同上 | 拦截面有多大：`mid + mid_deferred + low` 占 `total` 的比例就是"多问一句"的比例；`unrated` 突增说明模型停止自评、阈值策略已失效 |
| `intent_confidence_total` | counter | `band × action`（`action=proceed\|ask_confirm\|ask_missing_slot\|ask_intent_clarify`） | 三段判定成立时同时记"落段 + 实际动作"（确认回流轮单独记 `band=confirmed, action=proceed`） | [557](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L557)、[641](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L641) | 区分"落了中置信段但本轮先补槽"（`mid_deferred`）与"真的反问确认"（`mid`/`ask_confirm`）——只有后者消耗一轮 |
| `intent_confirmation_total` | counter | `result=accept\|reject` | 意图确认回流轮解析用户对"您是想办理 X 吗？"的回答：裸肯定词判 `accept`（含"是但…"的既肯定又否定保守判 `reject`） | [544](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L544)、[555](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L555) | §3.1 反问的实际价值：`reject / (accept+reject)` 即"模型自评分虚高被用户当场纠正"的比例，高于预期就该上调 `INTENT_CONFIDENCE_HIGH` |
| `slot_validation_fail_total` | counter | `reason=not_found\|ownership\|system` | 订单号业务校验（存在性 + 归属权）返回失败时，按话术归类 | [aftersales_langgraph.py:500](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L500) | 用户填错率（`not_found`）与越权尝试（`ownership`）；`system` 单涨说明库/依赖异常 |
| `guardrail_action_total` | counter | `action=block\|modify` | 输入护栏链命中：注入 BLOCK 中断本轮并转人工；PII MODIFY 脱敏后继续 | [aftersales_langgraph.py:429](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L429)、[444](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L444) | 注入攻击频次与脱敏发生量 |
| `ask_user_rounds` | histogram | 无 | 追问节点每生成一次追问，记当前累计补槽轮次 | [aftersales_langgraph.py:535](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L535) | 补槽轮次分布；`>2` 轮占比高说明追问话术问不到点上 |
| `node_latency_seconds` | histogram | `node=<9 个节点名>` | 节点**返回或抛异常后**由包装器计时（`perf_counter` 区间，含异常路径） | [aftersales_langgraph.py:256](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L256) | 慢节点定位：LLM 节点（`intent_recognition`）vs 工具节点（`query_order`/`check_logistics`/`search_policy`） |
| `decision_total` | counter | `decision=RETURN_SHIPPING\|AUTO_REFUND\|ESCALATE_HUMAN\|REJECTED\|RETRY_ASK\|OUT_OF_SCOPE`（全量取值见 [aftersales_langgraph.py:324](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L324)） | 节点输出或状态里出现 `decision` 字段时计数（包装器统一取，节点不必自报） | [aftersales_langgraph.py:259](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L259) | 自动通过 / 转人工 / 拒绝比例——成本与体验核心看板 |
| `checkpoint_fork_total` | counter | 无 | 一轮串行执行结束后统计该 `thread_id` 的 leaf checkpoint 数，`>1` 时 +1 | [aftersales_langgraph.py:1063](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1063) | 会话串行化是否被绕过（§6.2 缺陷回归监控） |
| `react_ai_total_requests` / `react_ai_total_tokens` / `tool_call_counts` / `tool_error_counts` | 旧版兼容字段 | — | 依赖 `record_request()` / `record_tool_call()` | **当前无生产调用点**（仅测试触发），故实际恒为 0/空 | 需接入 ReAct 生命周期钩子后才有数据，见 [Observability.md](Observability.md) 的 `ObsHook` 范式 |

节点标签取值（`node_latency_seconds{node=...}` 的全部序列）：`intent_recognition`、`query_order`、`check_logistics`、`policy_evaluation`、`condition_decision`、`execute_action`、`human_escalation`、`send_notification`、`ask_user`——即 `build_aftersales_graph` 里 `instrument_node` 包裹的 9 个节点。

`intent_confidence_band_total` 只有上表列出的 7 个 `band` 序列：另两个段名 `not_gated`（本轮没有自评分，如护栏/越界之外的短路）与 `guardrail_blocked`（注入护栏在阈值策略之前短路）**只写进状态与日志的 `confidence_band` 字段，不产生 counter**——它们代表"阈值策略未参与"，计进分母会稀释拦截面读数。

---

## 3. 记录哪些日志事件

事件出口两种：`log_node(state, event, **fields)`（[aftersales_langgraph.py:235](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L235)）与直接 `logger.*`。关联键覆盖分三档，按 `thread_id` 检索时请注意：

| 档位 | 事件 | 携带键 |
|---|---|---|
| 会话三键齐全 | 所有 `node_completed`/`node_failed` 与节点内业务事件（`slots_extracted`、`ask_user_*`、`tool_call_*`、`guardrail_*`、`risk_assessment`、`action_execute_started` 等） | `thread_id` + `session_id` + `user_id`（`trace_of`） |
| 仅 `thread_id` | 轮次与检查点类（`session_turn_*`、`checkpoint_verified`/`checkpoint_fork_detected`/`checkpoint_fork_probe_failed`、`session_lock_*`）、抽槽归因 `llm_*` | `thread_id`（`thread_id_of` 优先取状态显式字段，缺省按 `{tenant}:{user}:{session}` 推导；抽槽函数只接收 `thread_id` 故 `llm_*` 无会话两键） |
| 无会话键（启动期/全局） | `checkpointer_ready`、`checkpointer_degraded`、`checkpointer_close_failed`、`session_lock_release_failed` | 仅 `backend`/`error`——归入进程级事件，不参与单会话还原 |

| 事件 | 级别 | 触发时机 | 关键字段 | 采集位置 |
|---|---|---|---|---|
| `session_turn_started` / `session_turn_finished` | INFO | 一轮图调用进入串行段 / 段结束（`finished` 附整轮耗时） | `decision`、`latency_ms` | [1058](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1058)、[1068](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1068) |
| `node_completed` | INFO | 每个节点正常返回 | `node`、`latency_ms`、`intent`/`confidence_band`/`decision`/`missing_slots`/`slot_rounds`/`final_reply`（截断 60 字，仅有值才带） | [267](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L267) |
| `node_failed` | ERROR | 节点抛异常（记录后原样抛出，不改行为） | `node`、`latency_ms`、`error` | [265](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L265) |
| `llm_slots_validated` | INFO | 抽槽结果通过 Pydantic 校验 | `via=tool_calls\|content_json` | [365](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L365) |
| `llm_slots_rejected` | WARNING | Schema 校验失败（降级归因） | `errors`（违规字段清单） | [369](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L369) |
| `llm_slots_unstructured` | WARNING | 回复里解析不出 JSON | `content_len` | [362](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L362) |
| `llm_extraction_failed` | ERROR | LLM 调用本身异常（路由器抛错） | `error` | [372](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L372) |
| `guardrail_blocked` | INFO | 注入护栏 BLOCK，本轮转人工 | `guardrail_action`、`violation`（规则标识）、`decision` | [431](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L431) |
| `guardrail_masked` | INFO | PII 护栏 MODIFY | `guardrail_action`、命中的规则 id | [445](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L445) |
| `out_of_scope_intent` | INFO | 意图判定 OTHER 越界出口 | `intent`、`decision` | [476](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L476) |
| `slots_extracted` | INFO | 意图节点完成一轮槽位合并 | 槽位摘要与 `missing_slots` | [506](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L506) |
| `slot_validation_failed` | INFO | 订单业务校验失败（已计入指标） | `reason_class`、`detail`（`order_id` 故意置空，不写用户输入的他人单号） | [501](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L501) |
| `ask_user_round` | INFO | 生成本轮追问话术 | `slot_rounds`、`missing_slots` | [569](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L569) |
| `ask_user_exhausted` | INFO | 补槽轮次达上限转人工 | `slot_rounds`、`decision` | [538](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L538) |
| `order_cache_hit` | INFO | 订单详情已由校验阶段缓存，跳过重复查询 | — | [591](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L591) |
| `tool_call_started` / `tool_call_failed` | INFO / INFO | 每个工具节点调用前 / 失败回流 | `tool`、`order_id`、`error`、`decision`（如 `RETRY_ASK`） | [596](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L596)、[602](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L602)、[625](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L625)、[645](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L645)、[656](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L656)、[758](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L758) |
| `risk_assessment` | INFO | 风控节点给出资格与金额判据 | `eligible`、`total_amount` | [682](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L682) |
| `decision_passthrough` | INFO | 上游决策透传，不重复判定 | `decision` | [673](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L673) |
| `action_execute_started` | INFO | 业务写操作前（退款/换货落库） | `decision`、`order_id`、`amount` | [727](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L727) |
| `checkpointer_ready` / `checkpointer_degraded` | INFO / WARNING | 检查点后端就绪 / 降级为内存（PG 连接或 setup 失败） | `backend`、`error` | [972](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L972)、[961](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L961) |
| `session_lock_degraded` / `session_lock_release_failed` | WARNING | advisory lock 取不到或释放失败（降级放行，不拒绝用户） | `thread_id`、`error` | [1001](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1001)、[1014](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1014) |
| `checkpoint_verified` / `checkpoint_fork_detected` | INFO / ERROR | 每轮 head 数正常 / 检出多 head 分叉 | `head_count`、`note` | [1067](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1067)、[1064](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1064) |
| `checkpoint_fork_probe_failed` | WARNING | 分叉探针 SQL 失败（不影响业务轮次） | `thread_id`、`error` | [1039](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1039) |

日志载荷统一结构：`{"timestamp","time"(UTC ISO-8601 ms),"level","message","logger","context","extra"}`，关联键与业务字段都在 `extra` 下。实测两行（节点事件带全三键、进程级事件不带）：

```json
{"timestamp": 1793286504.055989, "time": "2026-10-29T15:08:24.055+00:00", "level": "INFO", "message": "slots_extracted", "logger": "react-ai", "context": {}, "extra": {"thread_id": "default:USR_001:SESS_A", "session_id": "SESS_A", "user_id": "USR_001", "intent": "RETURN_AND_REFUND", "missing_slots": []}}
{"timestamp": 1793286504.055989, "time": "2026-10-29T15:08:24.055+00:00", "level": "INFO", "message": "checkpointer_ready", "logger": "react-ai", "context": {}, "extra": {"backend": "postgres"}}
```

---

## 4. 观测时机（一次会话的调用栈顺序）

| 顺序 | 阶段 | 产出 | 触发者 |
|---|---|---|---|
| ① | 进程启动、装配检查点 | `checkpointer_ready` / `checkpointer_degraded`；决定后续能否做分叉探针 | `open_checkpointer` |
| ② | 每轮请求入口 | 注入 `thread_id` → 进入 `serialize_session`（进程内排队 + advisory lock，锁不可用只记 `session_lock_degraded`）→ `session_turn_started` | `ainvoke_serialized` |
| ③ | 图执行，逐节点 | **节点后**统一 `node_completed` + `node_latency_seconds`（异常走 `node_failed` 并原样抛出）；节点内按 §2/§3 表各自补记业务事件与指标 | `instrument_node` + 节点体 |
| ④ | 串行段内、图返回后 | head 数探针 → `checkpoint_verified` 或 `checkpoint_fork_detected` + `checkpoint_fork_total` | `ainvoke_serialized` |
| ⑤ | 锁释放、本轮收尾 | `session_turn_finished`（整轮 `latency_ms` + 终态 `decision`）；锁连接关闭 | `ainvoke_serialized` |
| ⑥ | 进程/演示结束 | `format_metrics_report()` 打印六类指标汇总到 stdout | `main_async` |
| ⑦ | 运行期任意时刻 | `GET /metrics` 拉全量文本、`GET /metrics/snapshot` 拉 JSON——**读到的是当前进程内存累计值** | API 服务 |
| ⑧ | 验收/排障时按需 | `fork-check --thread <id>` 读 PG 检查点表，输出 `{heads, linear_history, fork_metric_total}` | `live_e2e_verify.py` |

采集粒度总结：**指标=每次逻辑事件一次（非采样）**；**日志=每节点一条 + 每类归因一条**；**分叉探针=每轮一次 SQL**。

---

## 5. 口径与限制（如实记录）

| 项 | 现状 | 影响与应对 |
|---|---|---|
| 指标存储 | 仅进程内存，无持久化、无跨进程聚合 | 多实例需各自被抓取后由 Prometheus 聚合；进程重启计数从 0 开始，`rate()` 已按 counter 重置处理 |
| 直方图分位数 | `count`/`sum` 全量累计，分位数基于**最近 1024 条样本**估算 | 内存有界；极长尾（>1024 次之前的慢尾）不进 p99 |
| 框架文本日志 | `react_ai.*` 模块用 stdlib logger，冒泡到同一 stderr，但为纯文本 | 字段检索/告警只对 JSON 事件可靠；统一 JSON 化属后续项 |
| 旧版指标字段 | `record_request` / `record_tool_call` 无生产调用点 | `react_ai_total_requests`、`total_tokens`、工具计数恒为 0；接 `ObsHook` 后才有数据 |
| Trace span | `AgentTracer` 为 OTel 骨架，当前无调用点 | 全链路串联靠 `thread_id`，不依赖分布式 trace；`OTEL_EXPORTER_OTLP_ENDPOINT` 尚未接通 |
| 分叉探针前置条件 | 仅当 PG advisory lock 连接可用时才查 head 数（`lock_conn is not None`） | 内存检查点或锁降级时该轮**不做分叉判定**，只可能通过 `session_lock_degraded` 看出；单机演示下 `heads` 恒不报错 |
| 事件级别语义 | `slot_validation_failed` / `tool_call_failed` / `guardrail_blocked` 目前为 INFO 级 | 按级别告警会漏；应以**事件名 + `reason_class`/`decision` 字段**为准（或后续统一提级为 WARNING） |
| 敏感数据 | 护栏只记规则标识与动作；PII 原文、未脱敏正文、越权尝试的他人订单号不入日志；`final_reply` 截断 60 字 | 日志可安全进集中系统；需要原文取证时另走受控审计通道 |
| 采样率 | 无采样，全量记录 | 高 QPS 下日志量为主要成本项，必要时按 `LOG_LEVEL` 收敛 DEBUG |

---

## 6. 常用查询

| 目的 | 命令 / 查询 |
|---|---|
| 还原一次多轮会话全轨迹 | `grep '"thread_id": "default:USR_001:SESS_TEST_002"' run.jsonl`（Loki：`{job="react-ai"} \| json \| thread_id="default:USR_001:SESS_TEST_002"`） |
| 降级触发率 | PromQL：`rate(intent_extraction_total{source="fallback"}[5m]) / rate(intent_extraction_total[5m])` |
| 越权尝试 | `rate(slot_validation_fail_total{reason="ownership"}[10m])`（突增按安全事件处理） |
| 慢节点归因 | `histogram_quantile(0.95, ...)` 不适用 summary 导出，直接读 `node_latency_seconds{node="intent_recognition",quantile="0.95"}` |
| 转人工占比 | `rate(decision_total{decision="ESCALATE_HUMAN"}[1h]) / rate(decision_total[1h])` |
| 串行化是否被绕过 | `increase(checkpoint_fork_total[1d])`，或按线程实测 `py scripts/live_e2e_verify.py fork-check --thread <thread_id>`（分叉退出码 1） |
| 指标全量自证 | `curl localhost:8000/metrics`、`curl localhost:8000/metrics/snapshot` |
| 抽槽结构路径分布 | `--stats stats.jsonl` 后按 `source` 分组计数（`tool_calls` / `content_json` / `keyword_fallback` / `rejected_or_error`） |

---

## 7. 相关文件

| 用途 | 文件 |
|---|---|
| 指标采集器 | [src/react_ai/observability/metrics.py](file:///d:/code/react-ai/src/react_ai/observability/metrics.py) |
| 结构化日志器 | [src/react_ai/observability/logger.py](file:///d:/code/react-ai/src/react_ai/observability/logger.py) |
| Trace 骨架（未接通） | [src/react_ai/observability/tracer.py](file:///d:/code/react-ai/src/react_ai/observability/tracer.py) |
| 埋点、串行化与分叉探针 | [examples/aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py)（`instrument_node` / `serialize_session` / `ainvoke_serialized` / `format_metrics_report`） |
| 指标端点 | [api/main.py](file:///d:/code/react-ai/api/main.py) |
| 遥测驱动与分叉自检 | [scripts/live_e2e_verify.py](file:///d:/code/react-ai/scripts/live_e2e_verify.py) |
| 开关配置 | [src/react_ai/config/settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py)（`LOG_LEVEL` / `ENABLE_SESSION_LOCK` / `SESSION_LOCK_TIMEOUT`） |
| 守护测试 | [tests/unit/test_observability.py](file:///d:/code/react-ai/tests/unit/test_observability.py)、[tests/unit/test_aftersales_observability.py](file:///d:/code/react-ai/tests/unit/test_aftersales_observability.py) |
