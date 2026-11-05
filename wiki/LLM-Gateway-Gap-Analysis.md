# LLM 网关模块企业级差距分析

> 适用对象：[src/react_ai/llm/](file:///d:/code/react-ai/src/react_ai/llm/) 模块（工厂、路由器、基类客户端、协议适配器）及其在项目中的实际使用情况。
> 本文档回答：**当前 LLM 接入层到了什么水准？距离企业级"多模型容灾网关 + 成本治理 + provider 限流熔断"还差什么、怎么补？**
> 更新时间：2026-11-04（已按代码现状核对各项落地状态）

---

## 1. 当前架构速览

```text
   api/routes/agent.py ──► create_llm_client()          ← 生产 API 主路径
   (get_agent L66-L77)         │  (client.py L11-L33)
                               ▼
                     单一 Provider 客户端                ← DeepSeek / Anthropic / OpenAI 三选一
                               │                            按 settings.llm.default_provider 决定
                               ▼
                     BaseModelClient.chat()             ← 内置指数退避重试 + 可选实例级 fallback
                                                        （L132-L226）

   ┌─────────────────────────────────────────────────────────┐
   │ LLMRouter（router.py L8-L66）：primary + fallbacks 顺序容灾 │  ⚠️ 仅 examples/aftersales_langgraph.py
   │ generate / stream_generate / stream_generate_full 三套降级   │     L206-L217 手工组装，
   └─────────────────────────────────────────────────────────┘     生产 API 路径从未实例化
                               ▲
   client.py L8 import 了 LLMRouter，但全文件从未使用（死引用）
```

### 已实现能力清单

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 统一基类 | [base_model_client.py](file:///d:/code/react-ai/src/react_ai/llm/base_model_client.py) `BaseModelClient` | provider/model/统计字段齐全，模板方法 `chat()` 统一入口 |
| 指数退避重试 | `chat()` L154-L204 | 区分 `RetryableError` / `NonRetryableError`，不可重试错误快速失败 |
| 超时保护 | `_do_chat_with_timeout` L228-L235 | 单次调用带超时模板方法（`ModelConfig.timeout_seconds` L73） |
| 实例级降级 | `self._fallback` L107-L111、L206-L220 | 重试耗尽后调备用客户端，内容加 `[降级模型 xxx]` 前缀 |
| 多模型容灾路由 | [router.py](file:///d:/code/react-ai/src/react_ai/llm/router.py) `LLMRouter` L8-L66 | primary + fallbacks 顺序尝试，覆盖 `generate` / `stream_generate` / `stream_generate_full` |
| 调用统计 | `get_stats()` L275-L291 | 累计调用 / 失败 / Token / 失败率 / 是否有备用 |
| 工厂函数 | [client.py](file:///d:/code/react-ai/src/react_ai/llm/client.py) `create_llm_client` L11-L33 | 按 `settings.llm.default_provider` 返回单客户端 |
| 协议适配器 | providers/openai / deepseek / anthropic | OpenAI 兼容与 Anthropic 协议转换，工具调用增量解析 |

**结论：单客户端层面的"重试/超时/统计"骨架扎实，属于"组件级完整"——但网关级容灾（LLMRouter）不在生产路径、配置中心与网关脱节、成本与限流熔断能力整体缺失，按企业级 LLM 网关标准衡量存在下述 8 项差距。**

---

## 2. 企业级差距分析（按重要性排序）

### 2.1 LLMRouter 不在主路径 —— 容灾能力是假象 ⚠️ P0 ❌ 未开始

**现状**：[client.py#L8](file:///d:/code/react-ai/src/react_ai/llm/client.py#L8) import 了 `LLMRouter` 但全文件从未实例化；[api/routes/agent.py#L72](file:///d:/code/react-ai/api/routes/agent.py#L72) 的 `get_agent()` 直接 `create_llm_client()` 拿单一 Provider 客户端。`LLMRouter` 全项目唯一实例化点在 [examples/aftersales_langgraph.py#L206-L217](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L206-L217)（示例代码手工组装主备）。生产 API 路径上**没有任何多模型容灾**：主 provider 宕机、区域故障或 API Key 失效时，所有请求直接失败。

**风险**：
1. 对外宣传"支持多模型容灾降级"，实际主路径单点，故障时全站不可用；
2. Router 与工厂两套创建方式并存，开发者极易走错路径（同 Prompts 2.1 孤岛问题）；
3. `client.py` 的死 import 掩盖了"已接入路由"的假象。

**企业级做法**：让工厂成为唯一组装点，按配置决定是否包 Router：

```python
# client.py
def create_llm_client(provider: Optional[str] = None, **kwargs) -> BaseLLMClient:
    primary = _build_one(provider or settings.llm.default_provider, **kwargs)
    fallbacks = [
        _build_one(p) for p in (settings.llm.fallback_providers or [])
    ]
    if fallbacks:
        return LLMRouter(primary=primary, fallbacks=fallbacks)
    return primary
```

配套在 [settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py) `LLMConfig` 新增 `fallback_providers: list[str]`（别名 `LLM_FALLBACK_PROVIDERS`，逗号分隔）。

**验收标准**：`create_llm_client()` 在配置了备用 provider 时返回 `LLMRouter` 实例；grep 全项目（examples 除外）无绕过工厂的手工组装；主 provider 抛 `RetryableError` 耗尽后请求自动由备节点承接并有降级日志。

---

### 2.2 双降级路径并存 —— Router 级与 Client 级 fallback 语义冗余 ⚠️ P1 ❌ 未开始

**现状**：降级逻辑存在两处——`LLMRouter.generate` 在客户端之间顺序 try（[router.py#L21-L31](file:///d:/code/react-ai/src/react_ai/llm/router.py#L21-L31)），而 `BaseModelClient.chat` 内部又持有 `self._fallback` 在重试耗尽后二次降级（[base_model_client.py#L206-L220](file:///d:/code/react-ai/src/react_ai/llm/base_model_client.py#L206-L220)），且降级后给内容拼 `[降级模型 xxx]` 字符串前缀。

**风险**：
1. 两层降级叠加时行为不可预测：Router 换客户端重试 → 该客户端内部又 fallback 一次，最坏情况重试次数呈乘积爆炸；
2. `[降级模型 xxx]` 前缀直接污染响应正文，下游若按 JSON 解析（结构化输出场景）必然失败；
3. 降级事件只散落在日志文本里，没有结构化字段可被监控消费。

**企业级做法**：降级职责统一收敛到网关（Router）层；`BaseModelClient._fallback` 标记 deprecated 或删除；降级事件通过结构化回调上报：

```python
# Router 层统一记录
logger.info("llm_degraded primary=%s to=%s trace=%s", primary.name, client.name, trace_id)
metrics.inc("llm_fallback_total", labels={"from": primary.provider, "to": client.provider})
```

响应正文保持原样，降级标记放入 `ModelResponse.raw_response` 或独立字段 `degraded_from`。

**验收标准**：全项目仅存在一条降级链路（网关层）；降级不修改 `content` 正文；`llm_fallback_total` 指标可在 `/metrics` 出口查询到。

---

### 2.3 timeout / max_retries 死配置 —— 配置中心与网关脱节 📦 P1 ❌ 未开始

**现状**：`settings.llm.timeout_seconds`（[settings.py#L20](file:///d:/code/react-ai/src/react_ai/config/settings.py#L20)）全项目唯一读取点在 [examples/aftersales_langgraph.py#L214](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L214)；`settings.llm.max_retries`（[settings.py#L21](file:///d:/code/react-ai/src/react_ai/config/settings.py#L21)）**全项目零读取点**。`create_llm_client()` 不向 `ModelConfig` 传递任何超时/重试参数，生产路径永远使用代码默认值（60s / 3 次）。

**风险**：
1. 运维通过 `LLM_TIMEOUT` / `LLM_MAX_RETRIES` 环境变量调参完全无效，排查线上超时问题时误导方向；
2. 超时/重试策略无法按 provider 差异化（Anthropic 慢、DeepSeek 快需要不同阈值）。

**企业级做法**：工厂组装时注入配置，`ModelConfig` 支持 provider 级覆盖：

```python
# client.py 工厂内
cfg = ModelConfig(
    timeout_seconds=settings.llm.timeout_seconds,
    max_retries=settings.llm.max_retries,
)
primary = _build_one(..., default_config=cfg)
```

**验收标准**：设置环境变量 `LLM_TIMEOUT=15 LLM_MAX_RETRIES=1` 后，实际 HTTP 请求超时与重试次数随之变化；单测断言工厂产出的客户端持有配置值（参考 [test_base_model_client.py#L105-L152](file:///d:/code/react-ai/tests/unit/test_base_model_client.py#L105-L152) 既有 `max_retries` 用例风格）。

---

### 2.4 无成本核算与预算控制 🔒 P1 ❌ 未开始

**现状**：统计只有累计 `_total_tokens`（[base_model_client.py#L262-L267](file:///d:/code/react-ai/src/react_ai/llm/base_model_client.py#L262-L267)），不区分 prompt/completion、无 provider×model 单价表、无按租户/会话归因、无预算阈值熔断。项目已有 [utils/token_counter.py](file:///d:/code/react-ai/src/react_ai/utils/token_counter.py) 但未接入 LLM 调用路径计费。

**风险**：
1. 无法回答"这个月哪个客户花了多少 token 费"，成本失控；
2. 异常循环（Agent 卡死反复调 LLM）没有预算熔断兜底，可一夜烧穿账户；
3. 降级到高价备模型时成本变化无感知。

**企业级做法**：
1. 单价表配置化：`LLM_PRICING = {"deepseek-chat": {"prompt": 0.001, "completion": 0.002}}`（元/千 token）；
2. `chat()` 成功后按 `usage.prompt_tokens / completion_tokens` 分别计费，写入 `MetricsCollector` 的 `llm_cost_total{provider,model,tenant}` 计数器；
3. 租户/会话级预算：超过阈值后拒绝新请求（402 语义）并告警。

**验收标准**：`/metrics` 出口可见 `llm_cost_total`；同一 trace_id 可汇总本次请求总成本；预算超限后请求被拒且有审计记录。

---

### 2.5 无网关侧 provider 限流 ⚠️ P1 ❌ 未开始

**现状**：`settings.security.enable_rate_limit`（[settings.py#L55](file:///d:/code/react-ai/src/react_ai/config/settings.py#L55)）仅在工具沙箱层生效（[tools/sandbox.py](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py)）；对 LLM 供应商 API 的调用没有任何 QPS/并发限制。

**风险**：
1. 突发流量直接打穿 provider 配额，收到 429 后进入重试风暴，雪崩放大；
2. 多租户共享同一 API Key，单租户大流量饿死其他租户，无公平调度。

**企业级做法**：网关层加令牌桶/信号量（按 provider 维度），与重试联动——被本地限流时排队而非透传打爆上游：

```python
class LLMRouter:
    async def generate(self, ...):
        async with self._semaphore:      # provider 级并发闸
            return await client.generate(...)
```

**验收标准**：压测下发往同一 provider 的并发请求数不超过配置上限；限流排队有超时兜底（避免请求悬挂）；`llm_rate_limited_total` 指标可观测。

---

### 2.6 无 LLM 语义缓存 📦 P2 ❌ 未开始

**现状**：`settings.security.enable_result_cache`（[settings.py#L54](file:///d:/code/react-ai/src/react_ai/config/settings.py#L54)）是**工具结果**缓存；LLM 调用层没有任何缓存——相同 system+messages 的重复请求每次全额计费。

**风险**：槽位抽取、意图分类等低温度高频场景存在大量完全相同的请求，重复付费且延迟不可优化。

**企业级做法**：网关层可选缓存：key = `hash(model + temperature + messages)`，value = `LLMResponse`，TTL 可配；仅对 `temperature <= 0.2` 且无流式要求的请求启用；命中计入 `llm_cache_hit_total`。

**验收标准**：重复相同请求第二次命中缓存（不产生 provider HTTP 调用）；缓存命中率指标可查；流式请求默认不缓存。

---

### 2.7 无 provider 健康探测与熔断 ⚠️ P2 ❌ 未开始

**现状**：`LLMRouter` 是纯"失败后才顺序试下一个"（[router.py#L21-L31](file:///d:/code/react-ai/src/react_ai/llm/router.py#L21-L31)）：没有失败率统计窗口、没有熔断打开/半开探测、没有把持续失败的节点临时摘除。工具层已有熔断器实现但未覆盖 LLM 调用。

**风险**：主节点完全宕机时，每个请求都要"等满超时 × 重试次数"才降级到备节点，P99 延迟 = timeout×retries 的乘积；节点恢复后也没有自动回切探测。

**企业级做法**：Router 内为每个节点维护滑动窗口失败率，超阈值熔断 N 秒（半开放行单个探测请求）；复用 [tools](file:///d:/code/react-ai/src/react_ai/tools/) 已有熔断器抽象。

**验收标准**：连续失败达到阈值后该节点在熔断窗口内被跳过（不再产生真实请求）；半开探测成功后自动回切；`llm_circuit_state{provider}` 指标可观测。

---

### 2.8 LLM 调用路径零 span 📊 P2 ❌ 未开始

**现状**：`BaseModelClient` 只有结构化日志（[base_model_client.py#L146-L149](file:///d:/code/react-ai/src/react_ai/llm/base_model_client.py#L146-L149) 等），无 OTel span；核心引擎 [engine.py](file:///d:/code/react-ai/src/react_ai/core/engine.py) grep `tracer|start_span|metrics` 零命中。项目已有 [observability/tracer.py](file:///d:/code/react-ai/src/react_ai/observability/tracer.py)，但全链路唯一打点层是工具沙箱（`tool.call.{name}`，[tools/sandbox.py#L263-L326](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py#L263-L326)）。`ModelResponse.trace_id` 字段存在（L59）却与 AgentTracer 的 span 体系没有打通。

**风险**：一次 Agent 请求在追踪视图里只剩工具 span，LLM 耗时、重试次数、降级路径全部不可见；"慢在模型还是慢在工具"无法归因。

**企业级做法**：`chat()` 入口包 `llm.call.{provider}` span，属性含 `model / attempt / latency_ms / tokens / degraded`；`trace_id` 与引擎 run_id 贯通。

**验收标准**：`/spans` 端点可见 `llm.call.*` span 且带 provider/model/tokens 属性；一次请求的 span 树中 LLM 节点与工具节点同树。

---

## 3. 实施优先级路线

| 优先级 | 差距项 | 一句话动作 |
|---|---|---|
| **P0** | 2.1 Router 不在主路径 | 工厂按 `fallback_providers` 配置组装 `LLMRouter`，消灭死 import |
| **P1** | 2.2 双降级冗余 | 降级收敛到网关层，去掉正文前缀污染，降级事件入指标 |
| **P1** | 2.3 死配置 | 工厂注入 `ModelConfig(timeout/max_retries)`，env 生效 |
| **P1** | 2.4 成本与预算 | provider×model 单价表 + 租户归因 + 预算熔断 |
| **P1** | 2.5 provider 限流 | 网关层按 provider 并发闸 + 排队超时 |
| **P2** | 2.6 语义缓存 | 低温非流式请求的 hash 缓存，命中率可观测 |
| **P2** | 2.7 健康探测熔断 | 滑动窗口失败率熔断 + 半开回切 |
| **P2** | 2.8 调用 span | `llm.call.*` span 贯通 trace_id |

> **落地核对**：每完成一项，须同步更新 [tests/unit/test_llm_protocol_adapters.py](file:///d:/code/react-ai/tests/unit/test_llm_protocol_adapters.py)（Router 容灾用例见 L341）与 [tests/unit/test_base_model_client.py](file:///d:/code/react-ai/tests/unit/test_base_model_client.py)（重试/超时用例见 L105-L152），并按 AGENTS.md 约定使用假数据（Mock provider），不依赖真实 LLM 服务。

---

## 4. 相关代码与文档索引

- 网关与容灾：[llm/router.py](file:///d:/code/react-ai/src/react_ai/llm/router.py)、[llm/client.py](file:///d:/code/react-ai/src/react_ai/llm/client.py)、[llm/base_model_client.py](file:///d:/code/react-ai/src/react_ai/llm/base_model_client.py)
- 生产接入点：[api/routes/agent.py#L66-L77](file:///d:/code/react-ai/api/routes/agent.py#L66-L77)
- 配置：[config/settings.py#L7-L21](file:///d:/code/react-ai/src/react_ai/config/settings.py#L7-L21) `LLMConfig`
- 示例（唯一 Router 实例化点）：[examples/aftersales_langgraph.py#L206-L217](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L206-L217)
- 可观测性基础：[observability/tracer.py](file:///d:/code/react-ai/src/react_ai/observability/tracer.py)、[observability/metrics.py](file:///d:/code/react-ai/src/react_ai/observability/metrics.py)
- 同类孤岛问题参照：[wiki/Prompts-Gap-Analysis.md](file:///d:/code/react-ai/wiki/Prompts-Gap-Analysis.md)（2.1 引擎孤岛）、[wiki/Guardrails-Observability-Gap-Analysis.md](file:///d:/code/react-ai/wiki/Guardrails-Observability-Gap-Analysis.md)（2.2 核心链零打点）
