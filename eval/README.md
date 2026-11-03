# 意图识别评测集使用文档（第二轮 §2.1）

> 目标：让"改提示词 / 换模型 / 调 Schema"从盲改变成**可量化的回归**。
> 被测对象：[examples/aftersales_langgraph.py](file:///D:/code/react-ai/examples/aftersales_langgraph.py) 的 `intent_recognition_node`（含输入护栏、function calling 抽槽、关键词降级、槽位合并、越界网关、订单业务校验）。
> 相关规划：[wiki/Intent-Recognition-Optimization.md](../wiki/Intent-Recognition-Optimization.md) §2.1。

---

## 1. 目录结构

```
eval/
├── datasets/
│   ├── intent_slots.jsonl        # 标注数据集，132 条，一行一条用例
│   └── order_fixture.json        # 订单归属桩数据（无 PG 时替代真实查库）
├── baselines/
│   ├── llm_deepseek_chat.json    # 真实 LLM 路径基线
│   └── fallback_keywords.json    # 关键词降级路径下限基线
├── run_intent_eval.py            # 回归评测执行器
└── README.md                     # 本文档
```

配套常量测试：[tests/unit/test_intent_eval_dataset.py](file:///D:/code/react-ai/tests/unit/test_intent_eval_dataset.py)——只校验数据集结构与覆盖度，不调 LLM、不连库，每次 PR 都能跑。

**副作用说明**：评测只执行意图节点，链路内仅有 `query_order` 只读查询，不触碰退款/工单/通知等写操作，可安全反复执行。

---

## 2. 一条命令上手

```bash
# 自动模式：有 LLM_API_KEY 走真实模型，否则评测关键词降级（下限基线）
py eval/run_intent_eval.py

# 明确指定评测路径
py eval/run_intent_eval.py --mode fallback          # 降级规则路径（0 成本、0 网络、完全确定性）
py eval/run_intent_eval.py --mode llm               # 真实 LLM 路径（114 条约 30 秒）

# 指定订单业务校验的数据源
py eval/run_intent_eval.py --db stub                # 用 order_fixture.json，无 PG 环境结果一致（CI 推荐）
py eval/run_intent_eval.py --db live                # 走真实 PG 的 query_order 工具

# 快跑与定位
py eval/run_intent_eval.py --category pii,injection # 只看某几层
py eval/run_intent_eval.py --ids C001,M003          # 只跑指定用例
py eval/run_intent_eval.py --limit 10               # 抽样冒烟
py eval/run_intent_eval.py --verbose                # 透传节点内部日志
```

`--db auto`（默认）先用一个已知订单号探测 PG，可用则 `live`、不可用则 `stub` 并打印提示；`--mode auto`（默认）按 `settings.llm.api_key` 是否存在选择路径。

---

## 3. 数据集 schema

一行一个 JSON 对象，字段如下：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 用例编号，前缀即分层：`C`/`M`/`V`/`MT`/`L`/`O`/`I`/`P` |
| `category` | ✅ | 分层名，见 §4 |
| `utterance` | ✅ | 本轮用户输入（补槽轮次里就是用户对追问的回答） |
| `user_id` | – | 操作用户，默认 `USR_001`；越权用例靠它制造归属不符 |
| `context` | – | 既往对话 `[{role, content}]`，还原多轮会话上下文 |
| `seed_slots` | – | 前几轮已确认的槽位，如 `{"intent": "RETURN_AND_REFUND"}`；节点合并策略为**已有值优先不被覆盖** |
| `seed_state` | – | 直接透传的初始状态，如 `{"order_validated_id": "ORD_CLOTH_001", "slot_rounds": 2}` |
| `expect` | ✅ | 断言集合，**出现即断言**（见下表） |
| `note` | – | 标注理由，供人复核 |

### `expect` 支持的断言项

| 断言项 | 语义 |
|---|---|
| `intent` | 期望意图枚举；`null` 表示"不该得出意图结论" |
| `intent_accept` | 可接受的等价结论集合。用于话术本身歧义的场景（"想退掉"判退货退款或仅退款都算对），`null` 也可入列 |
| `order_id` | 期望订单号；`null` 是**反幻觉断言**：不得凭空产出，坏单号必须被业务校验剔除 |
| `reason_type` | 期望 `quality_issue` / `no_reason`；`null` 同上 |
| `missing_slots` | 缺失清单精确相等（排序后比较） |
| `missing_include` | 缺失清单包含式断言（其它槽位是否已填不作强约束） |
| `ask_user` | 是否进入追问分支。由图自己的 `route_slots()` 判定，不是看节点返回值猜的 |
| `guardrail` | `ALLOW` / `MASK` / `BLOCK`，护栏链的实际动作 |
| `decision` | 节点短路产出的决策（`OUT_OF_SCOPE` / `ESCALATE_HUMAN`） |
| `slot_error_contains` | 校验失败话术需包含的子串（如"不属于当前账号"） |
| `mask_tokens` | 发往模型的历史消息里必须出现的脱敏标记，且**不得残留任何原始手机号/身份证号** |
| `confidence_min` | 本轮自评分不得低于该值（§3.1 正向锚点：措辞明确的话术必须被判为高置信、直接放行） |
| `band` | 阈值策略落段，枚举 `high` / `mid` / `mid_deferred` / `low` / `confirmed` / `unrated` / `not_gated` / `out_of_scope` / `guardrail_blocked`（§3.1）。直接钉住"这条话术该不该被拦"，改阈值必然暴露 |

### 标注纪律

- 期望能抽到的订单号必须在 `order_fixture.json` 里，否则该用例在 `--db stub` 下永远不可能通过（常量测试会拦）。
- 断言 `ask_user: false` 且非越界/非阻断的用例，三个必要槽位必须都给出非空期望值——不然它本该触发追问（常量测试会拦）。
- 歧义话术用 `intent_accept` 表达，不要把"模型合理推断"标成错。
- 每层都要有**假阳性对照**（`injection`/`pii` 层里的 `ALLOW` 用例），防止"靠过度拦截刷满分"。

---

## 4. 分层设计与当前覆盖

| 分层 | 条数 | 考察点 |
|---|---|---|
| `complete` | 20 | 一句话槽位齐全 → 直答不追问 |
| `missing` | 18 | 部分缺失 → 精准触发追问，且缺的槽位不得被编造 |
| `multi_turn` | 16 | 带 `context` + `seed_slots` 的多轮补槽、跨轮合并、已校验订单不重复查库 |
| `colloquial` | 16 | 口语、方言、错别字（"坡了个洞""不中意""碎成渣"），规则路径的天然盲区 |
| `out_of_scope` | 16 | 越界 OTHER → `OUT_OF_SCOPE` 出口，不得触发任何售后工具 |
| `injection` | 12 | 9 条应 `BLOCK` + 3 条假阳性对照（防误拦正常售后诉求） |
| `pii` | 12 | 10 条应 `MASK` 且脱净 + 2 条假阳性对照（后四位、邮箱） |
| `order_validation` | 4 | 格式合法但越权/不存在的订单号 → 剔除槽位并回流追问，话术正确 |

合计 **114 条**，覆盖方案文档要求的 7 个分层并额外补了 `order_validation` 一层（§2.2 的越权回流此前无任何量化保护）。

---

## 5. 指标口径

| 指标 | 定义 |
|---|---|
| 用例通过率 | 单条用例**所有**断言项全绿的比例——CI 门禁用它 |
| 意图准确率 | 断言了 `intent` 的用例中命中（含 `intent_accept`）的比例 |
| 槽位 micro-F1 | 三个关键槽位汇总：命中为 TP；期望有值而实际漏抽/错抽为 FN；实际产出了期望之外的值（幻觉、坏单号未剔除）为 FP |
| 追问触发正确率 | `ask_user` 断言的命中率。漏追问=越权/编造信息进下游；过追问=体验差 |
| 注入拦截率 | 期望 `BLOCK` 的用例里实际 `BLOCK` 的比例 |
| PII 脱敏触发率 / PII 脱净率 | 前者=护栏动作正确，后者=**发往模型的那条消息**里确实只剩掩码、无原始号码 |
| 误拦截率（越低越好） | 期望非 `BLOCK` 却被拦的比例 |
| 反幻觉率 | 期望为 `null` 的槽位实际保持为空的比例 |
| 缺失清单 / 决策出口 / 校验话术准确率 | `missing_slots` / `decision` / `slot_error_contains` 断言命中率 |
| 高置信断言达标率 | `confidence_min` 断言的命中率（§3.1 的正向锚点：明确话术不得被阈值拦住） |
| 置信度落段准确率 | `band` 断言的命中率（§3.1 的三段收敛是否按当前阈值生效） |
| 节点耗时 P50/P95 | 单节点墙钟时间；`fallback` 模式恒为 0 |

分层表按 `category` 重复计算以上指标，**这就是"哪类用例退步了"的落点**。

### 置信度面板（§3.1）

报告尾部额外打印一张自评分分布表，把每条用例的自评分按"意图断言判对 / 判错 / 全部"三组汇总（样本数、均值、P10/P50/P90 与三段计数），并列出落段分布。它是阈值评审的原始材料：**判错那组的分数位置决定了低阈值该放在哪里，判对组的 P10 决定了高阈值往下压到哪一档会开始误伤。**

同一份数据可用 `--confidence-report <路径>` 落成 markdown 归档（逐条：用例号 / 分层 / 自评分 / 落段 / 意图断言 / 话术），并附一张"高阈值切点扫描表"（各切点下的误判放行数与正确直放数）与建议阈值。建议阈值只在误判样本 ≥5 条时才具备移动阈值的统计资格，否则报告显式提示"维持默认、待扩充错例后复标"。报告含合成 PII 话术原文，`eval/reports/` 已在 `.gitignore` 内，不入库。

---

## 6. 改动后如何量化（标准工作流）

```bash
# 1) 改动前：与已提交基线对比（基线文件已入库，无需先跑）。
#    注意口径要一致：eval/baselines/llm_deepseek_chat.json 是 mode=llm + db=live 记录的。
py eval/run_intent_eval.py --mode llm --db live --baseline eval/baselines/llm_deepseek_chat.json

# 2) 改提示词/换模型后重跑，直接看分层 Δ 与"退步用例"清单
py eval/run_intent_eval.py --mode llm --db live --baseline eval/baselines/llm_deepseek_chat.json

# 3) 确认是真实提升后，刷新基线并提交
py eval/run_intent_eval.py --mode llm --db live --record-baseline eval/baselines/llm_deepseek_chat.json
```

口径不一致（`llm` 比 `fallback`、`live` 比 `stub`）时脚本会在对比段打印告警，跨路径的百分比没有可比性。若 CI 没有 PG，请固定用 `--db stub` 并另存一份同口径基线（如 `eval/baselines/llm_deepseek_chat_stub.json`），首次 `--record-baseline` 生成后入库。

输出里的"与基线对比"给出每个分层的通过率 Δ（▲▼）与**逐条用例的 pass→fail / fail→pass 翻转清单**，退步是哪一类一眼可见。全量真实模型评测约 30 秒，满足"5 分钟内量化"的验收标准。

用 `--ids/--limit/--category` 跑子集时，样本数与基线不一致的分层会显示"样本不同,跳过"，不输出误导性 Δ。

### 标定置信度阈值（§3.1 专用流程）

```bash
# 关掉拦截、只看自评分分布（灰度前的第一步：先量化"如果拦，会拦掉谁"）
py eval/run_intent_eval.py --mode llm --db stub --no-confidence-gate \
   --confidence-report eval/reports/confidence-off.md

# 临时换一组阈值跑影响面（不改 settings，只作用于本次进程）
py eval/run_intent_eval.py --mode llm --db stub --confidence-low 0.5 --confidence-high 0.75

# 阈值改动落库前：同口径重跑 + 重录基线（confidence_low/high 已写入报告 meta，跨阈值对比会被告警）
py eval/run_intent_eval.py --mode llm --db stub --baseline eval/baselines/llm_deepseek_chat.json
py eval/run_intent_eval.py --mode llm --db stub --record-baseline eval/baselines/llm_deepseek_chat.json
```

判据：切点扫描里"误判放行"应为 0 才允许下调 `INTENT_CONFIDENCE_HIGH`；`band` 断言（当前 16 条：8 条 `high` 直放 + 6 条 `mid` 需确认 + 2 条 `out_of_scope`）必须全绿，任何阈值改动把已标注"该确认"的话术放进 `high`，都会在这里翻红而不是在线上翻车。

---

## 7. CI 接入

```bash
# 每次 PR 必跑：数据集完整性（0 依赖、毫秒级）
py -m pytest tests/unit/test_intent_eval_dataset.py -q

# 提示词 / Schema / 模型配置变更的 PR：降级路径分数门禁（无需 key、无需 PG）
py eval/run_intent_eval.py --mode fallback --db stub --fail-under 0.75

# 有 LLM_API_KEY 的流水线再加一道：真实模型路径 + 基线回归
py eval/run_intent_eval.py --mode llm --db stub \
    --baseline eval/baselines/llm_deepseek_chat_stub.json --fail-on-regression
```

退出码：`0` 通过；`1` 相对基线退步；`2` 低于 `--fail-under` 阈值。报告 JSON 可另存：`--json eval/reports/pr_123.json`（该目录未入库，属临时产物）。

当前仓库尚无 CI 配置文件；上面的命令可直接挂到任一 CI（GitHub Actions / 内部流水线）作为两个 step——有 `LLM_API_KEY` 的流水线跑真实模型路径，没有的跑降级路径并把阈值调低到 §8 的下限分数附近。

---

## 8. 当前基线（2026-10-30 重录，含 §3.1 置信度契约）

| 指标 | 真实 LLM（deepseek-chat） | 关键词降级（下限基线） |
|---|---|---|
| 用例通过率 | **92.1%** (105/114) | **77.2%** (88/114) |
| 意图准确率 | 98.1% | 87.6% |
| 槽位 micro-F1 | 98.3% | 93.4% |
| 追问触发正确率 | 94.7% | 86.0% |
| 注入拦截率 | 66.7% (6/9) | 66.7% (6/9) |
| PII 脱敏触发率 / 脱净率 | 100% / 100% | 100% / 100% |
| 误拦截率 | 0% | 0% |
| 反幻觉率 | 89.9% | 94.2% |
| 高置信断言达标率 / 落段准确率 | 100% (8/8) / 100% (16/16) | 100% (8/8) / 62.5% (10/16) |
| 节点耗时 P50/P95 | 1899ms / 3631ms | 0 / 0 |

两份基线均以 `--db stub` 记录（CI 无 PG 也能同口径复现），`meta` 里带 `confidence_low=0.5 / confidence_high=0.8`，跨阈值运行会被对比段告警拦下。真实链路口径（`--db live`）另有 [../wiki/Intent-Recognition-E2E-Report.md](file:///d:/code/react-ai/wiki/Intent-Recognition-E2E-Report.md)。

分层通过率（LLM / fallback）：`complete` 100/75、`multi_turn` 100/87.5、`out_of_scope` 93.8/100、`pii` 100/75、`colloquial` 87.5/50、`missing` 83.3/77.8、`injection` 75/75、`order_validation` 100/75。

`band: mid` 与随之而来的 `ask_user: true` 是按 LLM 自评分标注的（模型对"只说退、没说退货"判 0.75），降级路径的规则自评是确定值，因此这部分在 fallback 下必然翻红——它是下限基线的失分项而非新缺陷；`--mode fallback` 的 CI 门禁据此从 0.78 下调到 **0.75**。

真实模型路径的两条建议阈值：CI 门禁 `--fail-under 0.90`；降级路径 `--fail-under 0.75`（低于即说明规则兜底被改坏）。

---

## 9. 评测集暴露出的已知缺口（尚未修复，属后续波次）

这些不是评测器的 bug，是被它量化出来的真实缺陷：

1. **注入护栏正则覆盖不足**：`PromptInjectionGuardrail` 漏掉带连接词的变体——"忽略之前**的**指示"、"ignore **the** above instructions"（`injection.py:25-30` 的模式不允许中间虚词）。当前注入拦截率 66.7%，`I001/I004/I009` 恒红，修复后自动转绿。
2. **降级路径把手机号当订单号**：`fallback_extract_slots` 的正则 `ORD[_\-A-Z0-9]+|\b\d{8,}\b` 会先命中 11 位手机号，导致含 PII 的原始文本（降级路径按"入数据库原文"双轨原则处理）抽出错误订单号并被业务校验剔除——`P002/P003/P007` 在 `fallback` 模式下失败。
3. **`reason_type` 过度推断**：面对"有点问题""不好使""小问题"这类模糊表述，模型直接判 `quality_issue` 而非追问，跳过应有的补槽。§3.1 的门禁只判**意图**置信度，不覆盖槽位级过度推断，因此这条仍未消除（反幻觉率 LLM 89.9% < 降级 94.2%，也是同一原因）；要治它需要把置信度断言扩展到 `reason_type` 槽位或改提示词。
4. **关键词路径无法区分仅退款/退货退款**：任何含"退"的话术一律 `RETURN_AND_REFUND`，且"换一双/换个"不命中 `换货|换一个|换新`——`complete`/`colloquial` 层的主要失分点。§3.1 落地后这条多了一层表现：规则自评对"退"类话术给 0.9，落 `high` 段直接放行，而同一句话 LLM 自评 0.75 落 `mid` 段要先确认意图，于是 `band: mid` 的 6 条锚点在降级路径恒红（§8 的落段准确率 62.5%）。把裸"退/退款"的规则自评降到 0.7 可让两条路径一致，但那等于用置信度掩盖关键词分拣本身的缺陷，故留待本条正解（区分 `REFUND_ONLY`/`RETURN_AND_REFUND`）时一并处理。

---

## 10. 与后续波次的关系

- **§3.1 置信度阈值**：阈值不能拍脑袋，`confidence` 字段接入后用本评测集标定（按分层看"低置信度直接执行"的误判率）。
- **§3.2 换意图挂起**：`MT011` 按当前"已有槽位优先"合并策略标注，改造后需重标（用例 `note` 已写明）。
- **§3.4 意图体系扩展**：新增意图大类时必须同步扩充分层用例，否则新类别处于无回归保护状态。
- **§2.2 真实链路验证**：`--db live --mode llm` 即为真实模型 + 真实 PG 的评测入口。
