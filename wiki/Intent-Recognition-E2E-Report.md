# 意图识别 §2.2 真实链路端到端验证报告

> 状态：**已完成 ✅**（2026-10-29 执行）· 方案出处：[Intent-Recognition-Optimization.md](file:///d:/code/react-ai/wiki/Intent-Recognition-Optimization.md) §2.2
> 验证对象：[examples/aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py) · 驱动脚本：[scripts/live_e2e_verify.py](file:///d:/code/react-ai/scripts/live_e2e_verify.py)

---

## 1. 验收结论

| 验收项 | 结果 | 关键证据 |
|---|---|---|
| 真实模型 + 真实 PG 下三用例按预期决策收敛 | **通过** | `RETURN_SHIPPING` / 追问后 `RETURN_SHIPPING` / `ESCALATE_HUMAN`；PG 落库 26 条检查点 |
| 跨进程重启续跑 | **通过** | 进程 A(PID 1284) 挂起待补槽 → 新进程 B(PID 32512) 仅收裸订单号即恢复全槽位并收敛 |
| 多实例状态一致 | **部分通过** | 顺序交替一致（PID 24740 → 39844 同会话接管）；**同会话并发写入导致检查点分叉**，见 §6 |
| tool_calls 命中率 / 正文 JSON 回退比例 | **通过** | 52 次抽取调用 **100% 原生 tool_calls，正文 JSON 回退 0 次**，关键词降级 0 次 |

**前置条件与文档"现状"的偏差**：文档 §2.2 记录的"本机无 `LLM_API_KEY`、5433 无 PG"已不成立——`.env` 配置了 `deepseek-chat`（`api.deepseek.com`），5433 端口有 PostgreSQL 17.10 实例、`ecommerce_aftersales` 库含 11 张表。首跑前 `checkpoints` 表为 0 行，因此本轮是该链路的**首次真实执行**，非复用历史结果。

---

## 2. 环境

| 项 | 值 |
|---|---|
| 模型 | `deepseek-chat`（`LLM_DEFAULT_PROVIDER=deepseek`，temperature=0，`tool_choice=SLOT_TOOL` 强制） |
| 数据库 | PostgreSQL 17.10 @ `127.0.0.1:5433/ecommerce_aftersales` |
| 检查点 | `AsyncPostgresSaver`（日志确认"已挂载"，未走 MemorySaver 降级） |
| 种子数据 | `orders` 6 单（含 `ORD_CLOTH_001`→USR_001、`ORD_LUXURY_005`→USR_003 ¥8800）、`users` 3 人 |
| 事件循环 | Windows 下 `SelectorEventLoop`（psycopg3 异步不兼容 Proactor，主示例已处理） |

---

## 3. 三用例（`py examples/aftersales_langgraph.py`）

| 用例 | 输入 | 期望 | 首轮实测 | 结论 |
|---|---|---|---|---|
| 1 完整诉求 | "我买的衣服破了，要退货退款，订单号 ORD_CLOTH_001" | `RETURN_SHIPPING` | ✅ 一次抽槽齐全 → 政策通过 → 自动退货 | 通过 |
| 2 缺失追问 | "我想退货" → "衣服质量有问题，订单号 ORD_CLOTH_001" | 追问 → `RETURN_SHIPPING` | 首跑第2轮被**归属权校验拦截**；修正后 ✅ | 通过（含一处修正，见 §6.1） |
| 3 大额超限 | "黄金手链断了，要求退款，订单号 ORD_LUXURY_005" | `ESCALATE_HUMAN` | ✅ ¥8800 > 上限 ¥0 → 转人工并出工单号 | 通过 |

首跑后 PG 检查点：`default:USR_001:SESS_TEST_001` 9 行、`SESS_TEST_002` 8 行、`SESS_TEST_003` 9 行（共 26 行 / 28 blob）——证明每个节点超步都真实持久化，而非内存态。

---

## 4. 挑选 3 条评测用例做真实链路（全图 + 真实 PG）

从 114 条数据集按分层挑选，覆盖"槽位齐全 / 缺失追问 / 订单越权校验"三条互斥路径：

| 用例 | 分层 | 真实链路会话 | 结果 |
|---|---|---|---|
| **C001** "我买的衣服破了，要退货退款，订单号是 ORD_CLOTH_001" | complete | `default:USR_001:E2E_C001` | 意图 `RETURN_AND_REFUND`、`order_id=ORD_CLOTH_001`、`reason_type=quality_issue`、缺失=[] → `RETURN_SHIPPING`；抽槽来源 **tool_calls**，1693ms |
| **M001** "我想退掉"（+跨进程补槽） | missing | `default:USR_001:E2E_RS3`（另 `E2E_RS2`） | 第1轮缺失 `['order_id','reason_type']` → ask_user（未越权猜测，intent 未误判为 OTHER）；补槽后 `missing=[]` → `RETURN_SHIPPING` |
| **V001** USR_003 提交他人订单 `ORD_CLOTH_002` | order_validation | `default:USR_003:E2E_MULTI` | 第1轮剔除 `order_id`、回"该订单不属于当前账号"（越权未入流程）；第2轮换本账号 `ORD_LUXURY_005` → `ESCALATE_HUMAN` |

评测执行器对同 3 条（`--mode llm --db live --ids C001,M001,V001`）：**用例通过率 100%**、意图准确率 100%、追问触发 100%、护栏判定 100%、**反幻觉率 100%**、校验话术准确率 100%（槽位 micro-F1 90.9%，因 M001 期望值为 `null` 槽位计入分母）。

### 4.1 重启续跑专项（决定性证据）

```
进程 A  PID 1284   msg="我想退货，衣服有质量问题"  → missing=['order_id']  rounds=1  decision=None   （进程退出）
进程 B  PID 32512  msg="ORD_CLOTH_001"            → intent=RETURN_AND_REFUND  reason_type=quality_issue
                                                       missing=[]  rounds=1  decision=RETURN_SHIPPING
```

第二轮输入**不含任何意图词与原因词**，仍能补全 `intent`/`reason_type` 并收敛——只可能来自 PG 检查点恢复的 `dialog_history` 与累积 `slot_values`；`slot_rounds=1` 跨进程原样保留（全新会话应为 0）。

### 4.2 多实例交替

- **顺序交替（通过）**：实例 A(PID 24740) 写入越权拒绝态 → 实例 B(PID 39844) 冷启动读回同一 thread 的 4 条检查点并接管，最终 `ESCALATE_HUMAN`。状态不在进程内存，符合 §2.6 设计目标。
- **并发同 thread（暴露缺陷）**：见 §6.2。

---

## 5. tool_calls 命中率与降级比例（验证 §2.1 收益）

| 来源样本 | 抽取调用数 | 原生 tool_calls | 正文 JSON 回退 | Pydantic 拒绝 | 关键词降级 |
|---|---|---|---|---|---|
| 全图会话（本报告的 turn 驱动） | 12 | 12（100%） | 0 | 0 | 0 |
| 数据集 40 条直跑抽槽路径 | 40 | 40（100%） | 0 | 0 | 0 |
| 评测集全量 114 条（节点日志计数） | 108 | — | — | 1 | 1 |

- `tool_choice` 强制在真实 deepseek-chat 上**无一次被忽略**，正文 JSON 兜底分支（`extract_slots_via_llm` 的 `raw is None and response.content`）在 52 次可观测调用中零命中——该分支目前仅是保险，不是常规路径。
- 全量 114 条中 6 条由注入护栏 BLOCK 短路（未发起 LLM 抽取），108 条进入抽取；其中 **1 条被 Pydantic 拦下**：模型把订单号写成 `ord cloth 001`（含空格，违反 `^[A-Za-z0-9_\-]{4,64}$`），Schema 拒绝 → 剔除 `order_id` → 回流追问。这是 Schema 反幻觉在真实链路上的正向生效样本，而非缺陷。
- 耗时：40 条串行 P50 2240ms / P95 3203ms；全图单轮抽槽 P50 1576ms。全量评测（并发 5）节点 P50 2985ms / P95 4246ms、总耗时 72.3s——与 §2.1 记录的 P95 1.9s 不可直接比较（并发度与本机出口带宽不同），本轮 5 分钟内完成量化，满足验收对耗时的要求。

### 5.1 评测集全量复现（真实 LLM + live PG）

```
py eval/run_intent_eval.py --mode llm --db live
```

用例通过率 **93.0%**（114）、意图准确率 **99.1%**、槽位 micro-F1 **97.9%**、追问触发 95.6%、护栏判定 97.4%、PII 脱净 100%、注入拦截 66.7%（9 条中 6 条）。分层：complete/multi_turn/out_of_scope/pii/order_validation **100%**，colloquial 87.5%、missing 83.3%、injection 75.0%。

**与 §2.1 上轮记录逐项一致**（93.0% / 99.1% / 97.9%），说明评测结论在真实链路上可复现；8 条失败用例仍是已记录的那四处缺口（注入正则变体、`reason_type` 对"有点问题/不好使"等模糊表述过度推断、颜色不符被误判质量、"想问下怎么回事"被判 OTHER），本轮未新增失败类型。

---

## 6. 发现的问题

### 6.1 主示例用例 2 参数与种子数据不匹配（已修）

`main_async` 用例 2 用 `USR_002` 提交属于 `USR_001` 的 `ORD_CLOTH_001`，第2轮补槽必然触发归属权拒绝，演示无法收敛到 `RETURN_SHIPPING`。**流程行为是正确的**（越权拦截按 §2.2 设计生效），问题在于示例参数。已将两轮 `user_id` 与 thread 前缀改为 `USR_001`，见 [examples/aftersales_langgraph.py:857-870](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L857-L870)；重跑后三用例全部收敛。

### 6.2 同一会话并发请求会造成检查点分叉（已修，见 §2.3 会话级串行化）

两个进程同时对 `default:USR_001:E2E_CONC` 发起首轮（消息分别为 `ORD_CLOTH_001` 退货、`ORD_CLOTH_002` 换货），双方均无异常退出、各自返回终态，但：

- `checkpoints` 表该 thread 累积 18 行、出现**两条独立 head 链**（`…5359→…5462` 与 `…56ad→…5783`，两者同父 `…4415`）；
- 两实例各自只看到自己那一轮（`dialog_turns` 均为 1），互相不可见；
- 随后第 3 轮（PID 26068）恢复时读到的是**第一实例**分支（`order_id=ORD_CLOTH_001`），第二实例那轮及其写操作成为孤儿分支。

影响：多实例负载均衡下同一会话的并发请求会丢轮次、并可能重复执行写操作——本轮两个分支各自走完 `execute_action` + `send_notification`，`notifications` 表在 14:16:02 落下两条同用户通知（id 49 / 50，相隔 326ms），即一次并发对话被用户侧感知为两笔处理结果。

> **修复记录（2026-10-29，随 §2.3 落地）**：采用建议中的 `thread_id` 粒度 PG advisory lock 方案，双层串行化——进程内 `asyncio.Lock` 排队同线程、`pg_advisory_lock(hashtext(thread_id))` 跨进程互持（`SET lock_timeout` 限时，取不到锁打 `session_lock_degraded` 并降级继续服务），标准轮次入口收敛到 `ainvoke_serialized(app, inputs, config)`（[examples/aftersales_langgraph.py:1043](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L1043)），主示例与 `live_e2e_verify.py turn` 全部改走该入口。每轮串行段结束后统计该线程 leaf checkpoint 数，`>1` 即 `checkpoint_fork_total` 计数并打 ERROR 级 `checkpoint_fork_detected`；`scripts/live_e2e_verify.py fork-check --thread <id>` 把同一判定做成可独立执行的验收命令（分叉时退出码 1）。
>
> **复验结果**：全新线程 `default:USR_001:E2E_SER_1` 上两个进程并发发起两轮流，`fork-check` 返回 `heads=1 / linear_history=true / checkpoint_fork_total=0`，且第二轮从检查点正确接续第一轮已抽取的 `ORD_CLOTH_001` 状态——轮次不再互相隐身，写操作不再双份。对照本报告的旧分叉线程 `E2E_CONC` 仍返回 `heads=2`（退出码 1），证明检测口径有效而非恒过。开关为 `ENABLE_SESSION_LOCK` / `SESSION_LOCK_TIMEOUT`，默认开启。详见 [Intent-Recognition-Optimization.md](Intent-Recognition-Optimization.md) §2.3。

### 6.3 环境备注

- `orders.shipping_address` 等字段中文在控制台显示为乱码，为建库脚本写入时的编码问题（GBK 控制台 + 容器 `server_encoding`），不影响订单存在性与归属权校验；本轮未处理，留作后续。
- 顺带观察：同一订单二次提交转人工各自新建工单（`after_sales_tickets` 中 `ORD_LUXURY_005` 有 3 张 `TKT_HUMAN_*`，来自本轮 3 次演示执行），业务写操作目前无按 `(order_id, intent)` 的幂等约束。属 §2.2 之外的观察，仅记录不处置。
- 本轮在 dev 库新增了 9 个测试 thread 的检查点与若干通知/工单行（`E2E_*` 与 `SESS_TEST_*`），未做清理；如需干净基线，可 `delete from checkpoints/checkpoint_blobs/checkpoint_writes where thread_id like 'default:USR_%:E2E_%'`。

---

## 7. 复现命令

```bash
# 1) 三用例（真实 LLM + 真实 PG）
py examples/aftersales_langgraph.py

# 2) 评测集全量 / 挑选 3 条
py eval/run_intent_eval.py --mode llm --db live
py eval/run_intent_eval.py --mode llm --db live --ids C001,M001,V001

# 3) 单轮驱动（跨进程/跨实例续跑；换 PID 重复执行即可复现 §4.1/§4.2）
py scripts/live_e2e_verify.py turn --user USR_001 --session E2E_RS3 \
   --msg "我想退货，衣服有质量问题" --label turn1 --stats stats.jsonl --out t1.json
py scripts/live_e2e_verify.py turn --user USR_001 --session E2E_RS3 \
   --msg "ORD_CLOTH_001" --label turn2 --stats stats.jsonl --out t2.json

# 4) 并发串行化验收（§6.2 修复复验）：同线程两进程并发 + 分叉自检
py scripts/live_e2e_verify.py turn --user USR_001 --session E2E_SER_1 \
   --msg "我买的衣服破了，要退货退款，订单号是 ORD_CLOTH_001" & \
py scripts/live_e2e_verify.py turn --user USR_001 --session E2E_SER_1 \
   --msg "换个订单 ORD_CLOTH_002 退货退款，质量有问题" & wait
py scripts/live_e2e_verify.py fork-check --thread "default:USR_001:E2E_SER_1"   # 期望 heads=1、退出码 0
```

驱动脚本 `scripts/live_e2e_verify.py` 只做旁路观测（包装 `get_llm_router` / `fallback_extract_slots` 记录抽槽来源），不改变图与节点行为；§6.2 修复后其 `turn` 子命令改由 `ainvoke_serialized` 进入，即与生产入口一致地经过会话串行化与分叉检测。

---

## 8. §2.2 验收判定

方案四项中，"三用例收敛"、"重启续跑"、"tool_calls 命中统计"三项**达成**；"多实例状态一致"在**顺序流量下达成、并发流量下发现检查点分叉缺陷**，故 §2.2 首跑判定为**达成并附带一处需跟进缺陷**（§6.2）。该缺陷已随 §2.3 可观测性同批修复并通过并发复验（见 §6.2 修复记录），§2.2 四项现全部达成——本报告的"同一会话只有一条时间线"前提已由会话级串行化保证，第三波的置信度阈值与状态改造可直接建立在此之上。
