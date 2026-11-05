# 护栏与可观测性模块企业级差距分析

> 适用对象：[src/react_ai/guardrails/](file:///d:/code/react-ai/src/react_ai/guardrails/) 与 [src/react_ai/observability/](file:///d:/code/react-ai/src/react_ai/observability/) 两个模块及其在核心执行链中的实际接入情况。
> 本文档回答：**护栏框架和追踪/指标框架各自到了什么水准？距离企业级"输入输出全量防护 + 全链路可观测"还差什么、怎么补？**
> 更新时间：2026-11-04（已按代码现状核对各项落地状态）

---

## 1. 当前架构速览

```text
  用户请求 ──► API / engine.stream(query)
                    │
                    ▼
        ❌ 原始 query 直接 append 为 USER 消息进 LLM（无护栏）
                    │
        ┌───────────┴────────────┐
        │ 走 PromptManager 渲染时 │ 仅覆盖声明了 user_input_variables 的变量
        │ _apply_input_guardrails│ ← prompts/base.py L186-L201 / L339-L342
        └───────────┬────────────┘
                    ▼
        工具 observation 返回后
        engine._apply_output_guardrails（L533-L548）
                    │  settings.security.enable_guardrails 总开关
                    ▼
        sanitize_observation（tool_output 链：PII + Injection，L25-L30）
                    │  ⚠️ 异常时静默放行原文（fail-open）
                    ▼
        进入下一轮 LLM 上下文 / 日志

  观测面：AgentTracer（内存 span + 可选 OTel）+ MetricsCollector（Prometheus 出口）
          唯一全链路打点层 = tools/sandbox.py 的 tool.call.{name} span
          ⚠️ 核心引擎 engine.py grep tracer|start_span|metrics 零命中
```

### 已实现能力清单

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 护栏基类 | [guardrails/base.py#L287-L365](file:///d:/code/react-ai/src/react_ai/guardrails/base.py#L287-L365) | `check()` 完整链路：启用判断、实例熔断、方向匹配、前后钩子、核心检测、统计、审计日志 |
| 枚举与结果模型 | base.py L20-L69 | `GuardrailDirection` / `GuardrailAction` / `GuardrailCheckResult` 等 |
| 注入检测 | [injection.py#L19-L74](file:///d:/code/react-ai/src/react_ai/guardrails/injection.py#L19-L74) | `PromptInjectionGuardrail` 输入侧注入/越狱检测，BLOCK |
| PII 脱敏 | [pii.py#L18-L78](file:///d:/code/react-ai/src/react_ai/guardrails/pii.py#L18-L78) | 输入侧身份证/手机号掩码、输出侧手机号掩码 |
| 敏感词审核 | [moderation.py#L16-L69](file:///d:/code/react-ai/src/react_ai/guardrails/moderation.py#L16-L69) | `ModerationGuardrail` 双向 BLOCK —— ⚠️ 除 `__init__.py` 与单测外零调用方（孤岛） |
| 工具输出链 | [tool_output.py#L25-L52](file:///d:/code/react-ai/src/react_ai/guardrails/tool_output.py#L25-L52) | 固定 PII+Injection 两件套，`sanitize_observation()` |
| 引擎输出侧接入 | [core/engine.py#L389-L397](file:///d:/code/react-ai/src/react_ai/core/engine.py#L389-L397)、L533-L548 | 工具成功后序列化并脱敏，`enable_guardrails` 总开关 |
| 链路追踪 | [observability/tracer.py#L64-L150](file:///d:/code/react-ai/src/react_ai/observability/tracer.py#L64-L150) | 内存 span + 可选 OTLP 导出（L75 真实读取配置），导出失败降级不阻断 |
| 指标采集 | [observability/metrics.py#L69-L215](file:///d:/code/react-ai/src/react_ai/observability/metrics.py#L69-L215) | 工具调用/耗时/prompt 渲染/token/熔断状态，`to_prometheus()` |
| 指标与审计端点 | [api/routes/observability.py#L12-L41](file:///d:/code/react-ai/api/routes/observability.py#L12-L41) | `/metrics`、`/metrics/snapshot`、`/audit`、`/spans` |
| 审计存储装配 | [config/settings.py#L59-L68](file:///d:/code/react-ai/src/react_ai/config/settings.py#L59-L68) + `build_audit_store_from_settings` | postgres / memory / es 三后端，依赖不可用降级内存 |
| 沙箱全链路打点 | [tools/sandbox.py#L263-L326](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py#L263-L326) | `tool.call.{name}` span 包裹鉴权/审批/熔断/限流/缓存/执行 |

**结论：护栏与观测两套框架本身"组件级完整"——基类、枚举、指标出口、OTLP 导出都是真的；但接入面严重残缺：输入侧几乎不设防、核心执行链不可观测、异常时静默放行。按企业级标准衡量存在下述 6 项差距。**

---

## 2. 企业级差距分析（按重要性排序）

### 2.1 用户输入侧零护栏 —— 合规红线 🔒 P0 ❌ 未开始

**现状**：核心引擎 `ReActAgent.stream()`（[engine.py#L92-L103](file:///d:/code/react-ai/src/react_ai/core/engine.py#L92-L103)）把原始 query 直接 append 为 `USER` 消息发往 LLM，**不经过任何护栏**。护栏链只覆盖两处：① `PromptManager` 渲染路径的 `user_input_variables`（[prompts/base.py#L186-L201](file:///d:/code/react-ai/src/react_ai/prompts/base.py#L186-L201)、L339-L342）；② 工具 observation 输出侧（engine L533-L548）。`ModerationGuardrail` 虽有完整实现，但 grep 确认除 [guardrails/__init__.py](file:///d:/code/react-ai/src/react_ai/guardrails/__init__.py) 与 [test_guardrails.py#L20](file:///d:/code/react-ai/tests/unit/test_guardrails.py#L20)、L442-L443 外**无任何调用方**——敏感词合规审核在生产路径上不存在。

**风险**：
1. 提示词注入/越狱文本（"忽略上述所有指示…"）直达模型，整个 ReAct 循环被劫持后可驱动工具执行危险操作；
2. 用户输入中的 PII 原文进入 LLM 上下文并被审计日志、指标快照记录，违反数据合规要求；
3. 敏感违规内容无拦截直接回流给用户，触碰生成式 AI 监管红线。

**企业级做法**：在引擎入口（或 API 层中间件）对 query 跑完整输入链：

```python
# engine.stream() 入口
from react_ai.guardrails.injection import PromptInjectionGuardrail
from react_ai.guardrails.moderation import ModerationGuardrail
from react_ai.guardrails.pii import PIIMaskingGuardrail

INPUT_CHAIN = GuardrailChain([
    PromptInjectionGuardrail(),   # BLOCK
    ModerationGuardrail(blocklist=load_blocklist()),  # BLOCK
    PIIMaskingGuardrail(),        # MASK
])

res = INPUT_CHAIN.check(query, GuardrailContext(request_id=run_id, direction=GuardrailDirection.INPUT))
if res.action == GuardrailAction.BLOCK:
    yield blocked_reply(res.reason); return
query = res.modified_content or query   # 脱敏后入上下文，原文仅存业务侧
```

遵循已落地的双轨原则：**发往 LLM 用脱敏文本，业务校验与存储用原始输入**。

**验收标准**：注入类 query 在引擎入口被 BLOCK 且返回合规话术；敏感词命中有审计记录；进入 `state.messages` 的用户消息中 PII 为掩码形态；`ModerationGuardrail` 不再是孤岛（有生产调用方）。

---

### 2.2 核心执行链零打点 —— trace 断裂 ⚠️ P1 ❌ 未开始

**现状**：grep [core/engine.py](file:///d:/code/react-ai/src/react_ai/core/engine.py) 中 `tracer|start_span|metrics|record_` **零命中**。LLM 调用、ReAct 步骤推进、护栏命中、HITL 挂起/恢复全部无 span。全项目唯一完整打点层是工具沙箱（[tools/sandbox.py#L263-L326](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py#L263-L326)）。`AgentTracer` 与 `MetricsCollector` 框架本身可用（OTLP 导出、Prometheus 出口均为真实实现），但主执行链没接。

**风险**：
1. 一次请求在 `/spans` 视图里只见工具节点，"慢在模型还是慢在工具/审批"无法归因；
2. 步骤数、挂起次数、恢复延迟等行为指标缺失，容量规划与 SLA 度量无数据支撑；
3. 已有的 tracer/metrics 投入产出为零，重复造轮子风险。

**企业级做法**：引擎关键节点包 span：

```python
with tracer.start_span("agent.run", attributes={"run_id": ..., "query_len": ...}):
    for step in range(max_steps):
        with tracer.start_span("react.step", attributes={"step": step}):
            with tracer.start_span("llm.call", attributes={"provider": ..., "msg_count": ...}):
                resp = await self.llm.generate(...)
        # 挂起/恢复同理：hitl.pause / hitl.resume
```

**验收标准**：`/spans` 端点可见 `agent.run → react.step → llm.call / tool.call` 完整 span 树；`agent_steps_total`、`hitl_pause_total` 指标出现在 `/metrics` 出口。

---

### 2.3 输出护栏异常静默降级原文 —— fail-open 无告警 📊 P1 ❌ 未开始

**现状**：`_apply_output_guardrails`（[engine.py#L533-L548](file:///d:/code/react-ai/src/react_ai/core/engine.py#L533-L548)）在护栏链抛异常时捕获后**直接返回原始文本**，既不计数、不告警，也没有降级标记。`settings.security.enable_guardrails`（[settings.py#L47](file:///d:/code/react-ai/src/react_ai/config/settings.py#L47)）为全局一刀切开关，关闭后无任何补偿控制。

**风险**：
1. 护栏实现 bug 或依赖规则加载失败时，PII/违规内容静默流入下一轮上下文、日志与最终回复——防护失效无人知晓；
2. 无 `guardrail_error_total` 指标，运维无法发现"护栏长期带病运行"。

**企业级做法**：区分"可容忍降级"与"必须阻断"：对 PII 掩码类失败可降级但必须 `metrics.inc("guardrail_failopen_total", labels={guardrail})` + WARNING 日志；对 BLOCK 类护栏失败应改为 fail-closed（截断该 observation 并标记不可信），或至少将原文替换为占位符。

**验收标准**：护栏异常路径有指标计数与结构化日志；单测用故意抛异常的桩护栏验证降级行为可观测；BLOCK 类护栏故障不再静默放行原文。

---

### 2.4 护栏链硬编码 —— 不可按场景配置装配 📦 P2 ❌ 未开始

**现状**：工具输出链固定为 PII+Injection 两件套（[tool_output.py#L25-L30](file:///d:/code/react-ai/src/react_ai/guardrails/tool_output.py#L25-L30)），不含 moderation；输入侧链（PromptManager 路径）同样在代码里写死。没有配置驱动的"场景 → 护栏组合"装配表，新增护栏需要改代码。

**风险**：不同业务场景（客服/内部分析/对外开放）需要的防护组合不同，硬编码导致要么过度拦截、要么防护不足；`BaseGuardrail` 的钩子/熔断/规则配置能力被链的组装方式拖累。

**企业级做法**：配置中心声明链：`GUARDRAIL_CHAINS = {"tool_output": ["pii","injection"], "user_input": ["injection","moderation","pii"]}`，启动时按名装配注册；护栏实例支持 `enabled` 与 severity/action 覆盖。

**验收标准**：不改代码即可为某场景增删链上护栏；链配置错误（未知护栏名）启动即报错。

---

### 2.5 span/指标驻留进程内存 —— 多实例不一致 📊 P2 ❌ 未开始

**现状**：`AgentTracer` 的 span 存内存列表（[tracer.py#L64-L150](file:///d:/code/react-ai/src/react_ai/observability/tracer.py#L64-L150)），`MetricsCollector` 为进程内计数器（[metrics.py#L69-L215](file:///d:/code/react-ai/src/react_ai/observability/metrics.py#L69-L215)）。OTLP 导出默认关闭（`otlp_endpoint` 默认 None，[settings.py#L62](file:///d:/code/react-ai/src/react_ai/config/settings.py#L62)）。多 worker 部署时 `/metrics` 每次请求命中不同进程，读数抖动；重启即丢全部历史。

**风险**：Prometheus 抓取到的是"随机某个 worker"的局部值，告警与看板失真；无法做跨实例聚合分析。

**企业级做法**：生产部署强制开启 OTLP 导出（trace 走 Collector）；指标侧或引入 pushgateway/共享存储（Redis 计数器），或明确"每 worker 独立抓取"的部署拓扑并在文档中声明。

**验收标准**：多 worker 下指标聚合方案有明确文档与配置校验（启动时检测 worker 数 > 1 且未配 OTLP 则告警）。

---

### 2.6 指标齐备但零告警联动 📊 P2 ❌ 未开始

**现状**：`MetricsCollector` 已导出工具失败率、耗时分布、熔断状态等（[metrics.py#L69-L215](file:///d:/code/react-ai/src/react_ai/observability/metrics.py#L69-L215)），`get_stats()` 有 `failure_rate`，但项目内没有任何告警规则、阈值定义或通知通道接入。且 `/metrics`、`/audit`、`/spans` 端点匿名可访问（见 [API-Service-Gap-Analysis.md](file:///d:/code/react-ai/wiki/API-Service-Gap-Analysis.md) 2.1 零认证）。

**风险**：护栏命中率飙升、工具熔断打开、LLM 失败率超阈值等异常只能靠人肉盯 `/metrics` 快照；观测端点本身还成为信息泄漏面。

**企业级做法**：随仓库提供 Prometheus 告警规则文件（错误率、P99 耗时、熔断 OPEN、护栏 BLOCK 命中率突增），接入现有通知渠道；观测端点加只读 token 或内网隔离。

**验收标准**：`alert_rules.yml` 覆盖至少 4 类阈值场景；观测端点未授权访问返回 401。

---

## 3. 实施优先级路线

| 优先级 | 差距项 | 一句话动作 |
|---|---|---|
| **P0** | 2.1 输入侧零护栏 | 引擎入口挂 Injection+Moderation+PII 输入链，消灭 ModerationGuardrail 孤岛 |
| **P1** | 2.2 核心链零打点 | agent.run / react.step / llm.call / hitl span 接入 AgentTracer |
| **P1** | 2.3 护栏 fail-open 静默 | 降级计数+告警，BLOCK 类改 fail-closed |
| **P2** | 2.4 链硬编码 | 配置驱动的"场景→护栏组合"装配表 |
| **P2** | 2.5 内存态观测数据 | 强制 OTLP / 共享计数方案，多实例拓扑声明 |
| **P2** | 2.6 零告警联动 | 告警规则文件 + 观测端点鉴权（联动 API 文档 2.1） |

> **落地核对**：每完成一项，须同步扩充 [tests/unit/test_guardrails.py](file:///d:/code/react-ai/tests/unit/test_guardrails.py)（输入链阻断/脱敏用例）与引擎侧集成用例（假护栏桩验证 fail-open 计数），按 AGENTS.md 约定全部使用假数据、不依赖外部 PG/Redis。

---

## 4. 相关代码与文档索引

- 护栏框架：[guardrails/base.py](file:///d:/code/react-ai/src/react_ai/guardrails/base.py)、[injection.py](file:///d:/code/react-ai/src/react_ai/guardrails/injection.py)、[pii.py](file:///d:/code/react-ai/src/react_ai/guardrails/pii.py)、[moderation.py](file:///d:/code/react-ai/src/react_ai/guardrails/moderation.py)、[tool_output.py](file:///d:/code/react-ai/src/react_ai/guardrails/tool_output.py)
- 观测框架：[observability/tracer.py](file:///d:/code/react-ai/src/react_ai/observability/tracer.py)、[observability/metrics.py](file:///d:/code/react-ai/src/react_ai/observability/metrics.py)、[api/routes/observability.py](file:///d:/code/react-ai/api/routes/observability.py)
- 接入点：[core/engine.py#L389-L397](file:///d:/code/react-ai/src/react_ai/core/engine.py#L389-L397)、L533-L548；[prompts/base.py#L186-L201](file:///d:/code/react-ai/src/react_ai/prompts/base.py#L186-L201)；[tools/sandbox.py#L263-L326](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py#L263-L326)
- 配置：[config/settings.py#L47](file:///d:/code/react-ai/src/react_ai/config/settings.py#L47) `enable_guardrails`、L59-L68 `ObservabilityConfig`
- 姊妹文档：[wiki/LLM-Gateway-Gap-Analysis.md](file:///d:/code/react-ai/wiki/LLM-Gateway-Gap-Analysis.md)（2.8 LLM 调用零 span 与本文 2.2 同根因）、[wiki/API-Service-Gap-Analysis.md](file:///d:/code/react-ai/wiki/API-Service-Gap-Analysis.md)（2.1 观测端点零认证）、[wiki/Prompts-Gap-Analysis.md](file:///d:/code/react-ai/wiki/Prompts-Gap-Analysis.md)（2.4 变量护栏集成）
