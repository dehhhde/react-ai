# 企业级 AI 中台能力域模型与 react-ai 差距分析

> 评估对象：本仓库 `react-ai`（v0.1.0）  
> 评估视角：企业级 AI 中台的“能力域完整性 + 平台化程度”  
> 评估原则：**只以仓库内可运行的代码与配置为证据，不以 README/设计文档的宣称作为落地依据**  
> 结论速览：当前成熟度约 **60.5/100**，定位为「可管控的智能体运行时内核」，距企业级中台还差 **接入可信、平台运营、资产治理** 三类能力。

---

## 1. 判定标准：什么才算“企业级 AI 中台”

一个系统要被称为企业级 AI 中台，至少要同时满足五条：

| # | 判据 | 含义 |
|---|---|---|
| 1 | 可复用 | 模型、工具、知识、编排能力以服务/资产形式沉淀，业务方无需重复造轮子 |
| 2 | 可管控 | 统一身份、租户隔离、权限、配额、审批、审计形成闭环，且身份不可被调用方伪造 |
| 3 | 可运营 | 资产版本化、灰度发布、评测回归、用量计量、成本归集、告警看板可日常运营 |
| 4 | 可扩展 | 多模型、多业务、多租户、多实例横向扩展，状态不锁死在单进程 |
| 5 | 可交付 | 有控制台/开放平台/SDK、SLA 与高可用方案，能对外承诺服务等级 |

### 1.1 状态口径

| 标记 | 含义 |
|---|---|
| ✅ | 已落地：代码可运行，有测试或明确接入路径，语义基本满足企业级要求 |
| ◐ | 部分落地：组件存在但只覆盖单实例/单场景，或缺管控面、缺运营面，不能直接对外承诺 |
| ❌ | 缺失：仓库内无对应实现，或仅有设计文档/占位骨架 |

### 1.2 评分方法

按能力项赋权：**P0 = 3 分、P1 = 2 分、P2 = 1 分**；状态折算 **✅=1、◐=0.5、❌=0**。

> 得分率 = Σ(权重 × 状态值) / Σ权重。该评分只衡量“中台能力完备度”，不代表代码质量。

---

## 2. 能力域模型与现状对照

能力域按 **A 接入体验 / B 模型能力 / C 智能体编排 / D 知识与记忆 / E 工具执行 / F 安全合规 / G 平台运营** 七域展开，共 47 项 L2 能力。

### A. 接入与体验域（权重 12，得分 4.5，38%）

| ID | 能力项 | 企业级要求 | 状态 | 现状与证据 |
|---|---|---|---|---|
| A1 | 统一 API 契约 | OpenAPI 规范、版本化、统一错误码 | ✅ | FastAPI 自动生成 OpenAPI，REST + SSE 双端点（[api/main.py](file:///d:/code/react-ai/api/main.py)、[api/routes/agent.py](file:///d:/code/react-ai/api/routes/agent.py#L102-L119)） |
| A2 | 流式会话体验 | 断线重连、心跳、事件续传、背压 | ◐ | 仅单次 SSE 推送，无 `Last-Event-ID` 续传/连接管理；[SSE-Connection-Manager-Design.md](file:///d:/code/react-ai/wiki/SSE-Connection-Manager-Design.md) 仍为设计稿 |
| A3 | 统一认证网关 | JWT/OAuth2/API Key 校验，身份签名下发 | ❌ | 路由无鉴权依赖；`tenant_id`/`user_id` 直接取自请求体（[agent.py#L84-L99](file:///d:/code/react-ai/api/routes/agent.py#L84-L99)），CORS 为 `*`（[main.py#L16-L22](file:///d:/code/react-ai/api/main.py#L16-L22)） |
| A4 | 管理控制台 | 模型/工具/Prompt/知识资产可视化管控 | ❌ | 仓库无前端与控制台代码 |
| A5 | 多渠道接入 | Web/IM/语音/工单系统统一接入 | ❌ | 无渠道适配层，仅 HTTP/SSE |

### B. 模型能力域（权重 16，得分 7，44%）

| ID | 能力项 | 企业级要求 | 状态 | 现状与证据 |
|---|---|---|---|---|
| B1 | 多厂商模型接入 | 统一抽象、协议适配、新增厂商低成本 | ✅ | OpenAI/DeepSeek/Anthropic 三 Provider（[llm/providers/](file:///d:/code/react-ai/src/react_ai/llm/providers)） |
| B2 | 路由与容灾 | 健康探测、权重路由、流式首包失败切换 | ◐ | `LLMRouter` 仅按主备顺序 try/except，无健康检查与权重（[llm/router.py#L8-L31](file:///d:/code/react-ai/src/react_ai/llm/router.py#L8-L31)） |
| B3 | 模型配额与限流 | 租户/用户/模型维度 QPS 与 token 配额 | ◐ | 仅工具级令牌桶（[tools/resilience.py#L177](file:///d:/code/react-ai/src/react_ai/tools/resilience.py#L177)），无 LLM 调用配额 |
| B4 | 用量与成本归集 | 按租户/业务/模型/版本归集 token 与费用 | ◐ | 单次响应有 `cost_usd`（[base_model_client.py#L43-L48](file:///d:/code/react-ai/src/react_ai/llm/base_model_client.py#L43-L48)）；指标仅全局 `total_tokens`，无多维账本 |
| B5 | 模型评测选型 | 评测集、指标、AB 对比、选型报告 | ❌ | 仅有 Prompt/意图评测，无模型横向评测 |
| B6 | 密钥治理 | KMS/Vault、轮换、最小权限 | ❌ | 密钥全部来自环境变量明文（[config/settings.py#L14-L21](file:///d:/code/react-ai/src/react_ai/config/settings.py#L14-L21)） |
| B7 | 语义缓存/模型缓存 | 相似问缓存、命中率与成本下降可度量 | ❌ | 只有工具结果缓存（[settings.py#L66](file:///d:/code/react-ai/src/react_ai/config/settings.py#L66)） |

### C. 智能体与编排域（权重 14，得分 10，71%）

| ID | 能力项 | 企业级要求 | 状态 | 现状与证据 |
|---|---|---|---|---|
| C1 | ReAct 运行时 | 步数/预算/超时熔断、取消、事件流 | ✅ | 完整状态机（[core/engine.py#L567-L646](file:///d:/code/react-ai/src/react_ai/core/engine.py#L567-L646)） |
| C2 | 生命周期与事件协议 | 标准化事件、钩子、审计埋点 | ✅ | `AgentEvent` + Lifecycle Hook（[core/lifecycle.py](file:///d:/code/react-ai/src/react_ai/core/lifecycle.py)、[events/base.py](file:///d:/code/react-ai/src/react_ai/events/base.py)） |
| C3 | 多智能体编排 | Supervisor/路由/协作可运行 | ◐ | 编排基类较完整，但 Supervisor/Router 为薄壳（[orchestration/supervisor.py](file:///d:/code/react-ai/src/react_ai/orchestration/supervisor.py)、[router.py](file:///d:/code/react-ai/src/react_ai/orchestration/router.py)） |
| C4 | 状态图工作流 | 节点/边/条件分支/持久化恢复 | ◐ | [orchestration/workflow.py](file:///d:/code/react-ai/src/react_ai/orchestration/workflow.py) 仍为占位实现 |
| C5 | 意图与槽位 | 意图识别、槽位抽取、多轮追问平台化 | ◐ | 能力实现在示例业务链路（[examples/aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py)），未沉淀为平台组件 |
| C6 | HITL 审批闭环 | 挂起/恢复/超时降级/幂等/授权 | ◐ | 状态机、哈希链、超时降级已实现（[approval/service.py](file:///d:/code/react-ai/src/react_ai/approval/service.py)、[store.py#L49-L90](file:///d:/code/react-ai/src/react_ai/approval/store.py#L49-L90)）；缺审批人鉴权与多级审批 |
| C7 | Agent 资产注册与版本 | Agent/工作流可注册、版本化、灰度 | ❌ | 无 Agent 注册中心与版本管理 |

### D. 知识与记忆域（权重 12，得分 9.5，79%）

| ID | 能力项 | 企业级要求 | 状态 | 现状与证据 |
|---|---|---|---|---|
| D1 | 会话记忆 | 多轮历史、窗口裁剪、token 预算 | ✅ | 滑动窗口 + 预算裁剪（[memory/short_term.py](file:///d:/code/react-ai/src/react_ai/memory/short_term.py)、[memory/base.py#L536-L606](file:///d:/code/react-ai/src/react_ai/memory/base.py#L536-L606)） |
| D2 | 长期语义记忆 | 事实沉淀、检索、遗忘权 | ✅ | `LongTermSemanticMemory` + `forget_user`（[memory/long_term.py](file:///d:/code/react-ai/src/react_ai/memory/long_term.py)） |
| D3 | 向量检索 | 向量库、embedding、租户隔离 | ✅ | pgvector + 内存实现，SQL 层强制 tenant/user 过滤（[memory/vector/pgvector.py#L78-L90](file:///d:/code/react-ai/src/react_ai/memory/vector/pgvector.py#L78-L90)） |
| D4 | 知识库工程 | 文档接入、解析切分、索引管理、召回评测 | ❌ | 有检索工具（[tools/builtin/retrieval.py](file:///d:/code/react-ai/src/react_ai/tools/builtin/retrieval.py)），无知识库生产链路 |
| D5 | 记忆治理 | TTL、租户硬闸、可观测、数据删除 | ✅ | 租户硬闸默认开启（[memory/base.py#L320-L492](file:///d:/code/react-ai/src/react_ai/memory/base.py#L320-L492)） |
| D6 | 用户画像沉淀 | 自动抽取、去重、置信度、人工校正 | ◐ | 事实写入有注入护栏（[engine.py#L427-L437](file:///d:/code/react-ai/src/react_ai/core/engine.py#L427-L437)），无抽取流水线与治理台 |

### E. 工具与执行域（权重 16，得分 13.5，84%）

| ID | 能力项 | 企业级要求 | 状态 | 现状与证据 |
|---|---|---|---|---|
| E1 | 工具注册与声明 | 声明式注册、Schema 导出、参数校验 | ✅ | `@tool` + `ToolRegistry`（[tools/base.py](file:///d:/code/react-ai/src/react_ai/tools/base.py)、[tools/registry.py](file:///d:/code/react-ai/src/react_ai/tools/registry.py)） |
| E2 | 安全沙箱 | 超时隔离、线程池卸载、异常审计 | ✅ | `ToolSandbox.run_safe` + `asyncio.to_thread`（[tools/sandbox.py](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py)） |
| E3 | 工具鉴权 | 对接 IAM，RBAC/ABAC，默认拒绝 | ◐ | 默认实现按租户白名单且**无白名单即放行**（[tools/auth.py#L25-L51](file:///d:/code/react-ai/src/react_ai/tools/auth.py#L25-L51)） |
| E4 | 韧性治理 | 熔断、限流、舱壁、缓存 | ✅ | 四件套齐全，但限流状态在进程内（[tools/resilience.py](file:///d:/code/react-ai/src/react_ai/tools/resilience.py)） |
| E5 | 幂等与重放保护 | 原子占位、跨实例生效、结果缓存 | ◐ | 三态原子占位已实现，但默认 `InMemoryIdempotencyStore`（[idempotency/store.py#L57-L105](file:///d:/code/react-ai/src/react_ai/idempotency/store.py#L57-L105)） |
| E6 | 内置安全工具 | bash 白名单、HTTP SSRF 防护 | ✅ | 参数数组化 + 白名单；SSRF 白名单网段（[tools/builtin/bash.py](file:///d:/code/react-ai/src/react_ai/tools/builtin/bash.py)、[http.py](file:///d:/code/react-ai/src/react_ai/tools/builtin/http.py)） |
| E7 | 工具市场 | 三方接入、签名、审核、版本、依赖治理 | ❌ | 无插件市场与审核发布流程 |

### F. 安全与合规域（权重 16，得分 10.5，66%）

| ID | 能力项 | 企业级要求 | 状态 | 现状与证据 |
|---|---|---|---|---|
| F1 | 输入侧护栏 | 用户原文进 LLM 前统一阻断/脱敏 | ◐ | 组件完备，但通用 ReAct 链路未接入；仅示例链路显式挂载（[aftersales_langgraph.py#L337-L338](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L337-L338)） |
| F2 | 工具输出护栏 | Observation 脱敏、间接注入扫描 | ✅ | `sanitize_observation`（[engine.py#L858-L873](file:///d:/code/react-ai/src/react_ai/core/engine.py#L858-L873)） |
| F3 | PII 与数据保护 | 掩码 + 加密存储 + 最小化留存 | ◐ | 仅正则掩码，无加密存储与留存策略 |
| F4 | 审批与审计 | 不可篡改、可回溯、超时降级 | ✅ | 哈希链审计账本 + 审计存储 memory/PG/ES（[observability/audit.py](file:///d:/code/react-ai/src/react_ai/observability/audit.py)） |
| F5 | 内容合规 | 敏感词/违规内容拦截、分级处置 | ◐ | `ModerationGuardrail` 存在但默认链路未启用（[guardrails/moderation.py](file:///d:/code/react-ai/src/react_ai/guardrails/moderation.py)） |
| F6 | 数据主体权利 | 查询/删除/导出/证明 | ◐ | 删除已具备，缺导出与备份/日志/索引的级联清理 |
| F7 | 合规报表 | 留存策略、审计报表、DPIA 支撑 | ❌ | 无合规报表与留存策略配置 |

### G. 平台工程与运营域（权重 19，得分 8.5，45%）

| ID | 能力项 | 企业级要求 | 状态 | 现状与证据 |
|---|---|---|---|---|
| G1 | 多租户隔离 | 身份可信 + 存储/审计/指标全链路隔离 | ◐ | 记忆层硬闸已实现，但租户身份由客户端自报，隔离可被伪造绕过 |
| G2 | 配置中心 | 分层配置、热更新、环境隔离、校验 | ✅ | Pydantic Settings + env 分层（[config/settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py)） |
| G3 | 可观测性 | Trace/Metrics/结构化日志/审计 | ✅ | OTel + Prometheus 端点（[observability/](file:///d:/code/react-ai/src/react_ai/observability)、[main.py#L34-L43](file:///d:/code/react-ai/api/main.py#L34-L43)） |
| G4 | 告警与 SLO | 阈值告警、SLO 看板、值班闭环 | ❌ | 只有指标导出，无告警规则与看板 |
| G5 | 计量计费 | 账单、结算、对账、预算控制 | ❌ | 无计费体系 |
| G6 | 发布与灰度 | 资产灰度、回滚、变更审计 | ◐ | Prompt 已具备版本/灰度/回滚（[prompts/manager.py#L33-L46](file:///d:/code/react-ai/src/react_ai/prompts/manager.py#L33-L46)），模型/工具/Agent 维度缺失 |
| G7 | 高可用与多实例 | 状态外置、分布式锁、优雅发布 | ◐ | 会话记忆支持 Redis，但 checkpoint/审批/幂等/限流/缓存均为进程内单例（[core/checkpoint.py#L39-L61](file:///d:/code/react-ai/src/react_ai/core/checkpoint.py#L39-L61)） |
| G8 | 评测与数据闭环 | 回归门禁、线上反馈、AB、badcase 回流 | ◐ | 有 Prompt/意图离线评测（[eval/](file:///d:/code/react-ai/eval)），无线上闭环 |
| G9 | 容量与调度 | 队列、并发配额、资源隔离、优先级 | ❌ | 仅工具级舱壁 |
| G10 | 开放平台 | API Key、开发者沙箱、配额、文档 | ❌ | 无开放平台与开发者治理 |

---

## 3. 成熟度评分

| 能力域 | 权重 | 得分 | 得分率 | 成熟度判断 |
|---|---:|---:|---:|---|
| A 接入与体验 | 12 | 4.5 | 37.5% | L1–L2：能调用，不可对外交付 |
| B 模型能力 | 16 | 7.0 | 43.8% | L2：能接入，缺配额与成本运营 |
| C 智能体与编排 | 14 | 10.0 | 71.4% | L3：运行时成熟，编排资产化不足 |
| D 知识与记忆 | 12 | 9.5 | 79.2% | L3：记忆治理较好，知识工程缺失 |
| E 工具与执行 | 16 | 13.5 | 84.4% | L3：执行面最扎实，状态未外置 |
| F 安全与合规 | 16 | 10.5 | 65.6% | L2–L3：组件齐全，接线不完整 |
| G 平台工程与运营 | 19 | 8.5 | 44.7% | L2：缺运营与多实例能力 |
| **合计** | **105** | **63.5** | **60.5%** | **L3 起步（可管控内核）** |

成熟度对照：`<40 L1 能跑` / `40–60 L2 可集成` / `60–80 L3 可管控` / `80–95 L4 可运营` / `>95 L5 可规模化复用`。

**一句话结论**：内核层（运行时、工具、记忆、审计）已达到 L3；短板集中在「接入可信」与「平台运营」，因此现在适合定位为 **Agent 运行时底座 / AI 中台执行引擎**，尚不宜直接对外称为“AI 中台”。

---

## 4. 差距清单

### 4.1 P0：不补齐就不能称为企业级中台

#### P0-1 统一身份认证与可信租户传递

- **现状**：API 无任何鉴权中间件；`tenant_id`、`user_id` 由客户端在请求体自报（[agent.py#L84-L99](file:///d:/code/react-ai/api/routes/agent.py#L84-L99)）；工具鉴权默认“无白名单即放行”。
- **缺口**：身份不可信 → 记忆层的租户硬闸可被伪造 `tenant_id` 绕过，会话历史可被越权读取。
- **补齐方案**：接入 JWT/OAuth2/API Key 网关，签名校验后注入 `RunContext`，禁止请求体覆盖身份；落地 RBAC + ABAC；审批、审计、指标接口同样纳入鉴权。
- **验收标准**：无凭证返回 401；伪造租户返回 403；跨租户查会话统一 404；有对应单测。

#### P0-2 有状态组件外置与多实例一致性

- **现状**：Checkpoint、审批、幂等、限流/熔断、结果缓存均为进程内单例（[core/checkpoint.py#L39-L61](file:///d:/code/react-ai/src/react_ai/core/checkpoint.py#L39-L61)、[approval/service.py#L21](file:///d:/code/react-ai/src/react_ai/approval/service.py#L21)、[idempotency/store.py#L57](file:///d:/code/react-ai/src/react_ai/idempotency/store.py#L57)）。
- **缺口**：多实例部署下 HITL 无法跨实例恢复，幂等失效可能导致重复退款/重复下单，限流失效。
- **补齐方案**：提供 Redis/PG 版本实现：checkpoint 落 PG；幂等与限流用 Redis + Lua/CAS；审批状态机分布式锁；SSE 连接状态外置。
- **验收标准**：双实例并发同幂等键仅执行一次；重启后可 resume 挂起运行；压测无重复写。

#### P0-3 LLM 用量配额与成本归集

- **现状**：只有工具级令牌桶；单次响应有 `cost_usd` 估算，指标只有全局 `total_tokens`。
- **缺口**：无法按租户/业务/模型核算成本，无法做配额控制与预算预警，无法支撑内部结算。
- **补齐方案**：建立用量账本（tenant/user/model/prompt_version/day），调用前预扣、调用后结算；超配额返回 429 或降级到低成本模型；接入计量看板。
- **验收标准**：配额耗尽有明确 429；费用可按租户与业务线归因；账本与供应商账单误差 < 1%。

#### P0-4 输入侧与最终答复护栏闭环

- **现状**：通用 ReAct 链路只在工具 Observation 上做 PII/注入清洗（[engine.py#L858-L873](file:///d:/code/react-ai/src/react_ai/core/engine.py#L858-L873)）；用户 query 护栏仅出现在示例链路（[aftersales_langgraph.py#L337-L338](file:///d:/code/react-ai/examples/aftersales_langgraph.py#L337-L338)）；最终答复未过护栏。
- **缺口**：通用 API 直接暴露注入面；PII 可经最终答案泄露；不满足强合规行业要求。
- **补齐方案**：在 `RunContext` 构造后、首次 LLM 调用前统一执行 `GuardrailChain`（注入 → 合规 → PII），阻断即短路并审计；最终答案出口再过 PII/合规护栏；恶意原文不落历史。
- **验收标准**：注入样本阻断率 100%；响应无 PII 明文；阻断事件可在审计中按 trace 回溯。

#### P0-5 密钥与敏感数据治理

- **现状**：LLM/DB/Redis/ES 密钥均为环境变量明文；PII 仅掩码（[config/settings.py#L14-L21](file:///d:/code/react-ai/src/react_ai/config/settings.py#L14-L21)）。
- **缺口**：无 KMS/Vault、无轮换、无敏感字段加密，不满足企业安全基线。
- **补齐方案**：抽象 Secret Provider（env/Vault/KMS 多实现）；密钥轮换不重启生效；敏感字段应用层信封加密或库内加密；日志字段级脱敏白名单。
- **验收标准**：仓库与运行配置无明文密钥；轮换演练通过；审计日志扫描无敏感明文。

#### P0-6 审批授权、职责分离与多级审批

- **现状**：审批状态机、哈希链、超时降级已实现，但 `decide()` 只记录 `decided_by`，不校验审批人身份、角色、租户归属（[approval/service.py#L78-L107](file:///d:/code/react-ai/src/react_ai/approval/service.py#L78-L107)）。
- **缺口**：任意调用方可通过审批接口放行资金/履约类写操作，审批形同虚设。
- **补齐方案**：审批人鉴权 + 租户隔离 + 角色校验；多级会签与金额分级；职责分离（发起人不得自批）；审批记录绑定人、角色、时间与决策理由。
- **验收标准**：越权审批 403；未授权角色无法审批；审批链可人工复核。

#### P0-7 资产治理与发布流程

- **现状**：Prompt 已有版本/灰度/回滚（[prompts/manager.py#L33-L46](file:///d:/code/react-ai/src/react_ai/prompts/manager.py#L33-L46)）；工具无版本，Agent/工作流无注册中心，无控制台与发布审批。
- **缺口**：无法回答“线上跑的是哪个版本、谁发布的、灰度多少、如何回滚”。
- **补齐方案**：建立 Prompt/Tool/Agent/知识库资产元数据注册表，发布状态机（草稿→评审→灰度→全量→回滚），变更留痕并可一键回滚；配套管理控制台。
- **验收标准**：任一线上资产可查版本、发布人、灰度比例；回滚后行为可验证。

### 4.2 P1：平台化必须补齐

| ID | 差距 | 现状 | 补齐要点 |
|---|---|---|---|
| P1-1 | 多智能体编排可用性 | Supervisor/Router/Workflow 为薄壳或占位 | 补齐执行逻辑、失败传播、状态持久化与恢复 |
| P1-2 | 知识库工程链路 | 有向量检索，无文档接入/切分/索引管理/召回评测 | 文档解析、切片策略、增量索引、召回率与命中率评测、知识权限 |
| P1-3 | 告警与 SLO | 只有 `/metrics` 导出 | 阈值规则、告警通道、SLO 看板、值班与升级路径 |
| P1-4 | 评测与数据闭环 | 离线 Prompt/意图评测 | 线上反馈采集、AB 实验、badcase 回流、回归门禁接入 CI |
| P1-5 | SDK 与开放平台 | 仅 HTTP/SSE | 多语言 SDK、API Key 管理、开发者沙箱、配额与限流、接入文档 |
| P1-6 | 工具市场与插件治理 | 内置工具 + 自注册 | 三方接入审核、签名验签、版本与依赖治理、下架机制 |
| P1-7 | 数据主体权利完整链路 | 有删除，无导出与级联清理 | 数据导出、备份/日志/向量索引级联清理、删除证明 |
| P1-8 | 审批与 HITL 运营化 | 有挂起/恢复接口 | 审批工作台、待办提醒、超时策略分级、批量审批 |

### 4.3 P2：规模化与差异化能力

| ID | 差距 | 补齐要点 |
|---|---|---|
| P2-1 | 语义缓存与成本感知路由 | 相似问缓存、按成本/延迟/质量路由 |
| P2-2 | 多模态能力 | 图像、语音、文档理解统一接入 |
| P2-3 | 容量与资源调度 | 优先级队列、租户并发配额、资源隔离 |
| P2-4 | 多区域与多语言 | 区域化部署、数据驻留、i18n 资产 |
| P2-5 | 合规报表工具化 | 留存策略、审计报表、DPIA 模板与证据包 |

---

## 5. 演进路线

### 阶段一：内核加固（补齐 P0-1 ~ P0-5）

目标：从“单实例框架”变成“可管控内核”。

- 身份网关 + 可信身份注入 + RBAC/ABAC
- 状态外置（Redis/PG）与分布式锁
- LLM 配额与成本账本
- 输入/输出护栏统一接线
- 密钥托管与敏感数据加密

出关标准：多实例部署可用；身份不可伪造；注入与 PII 双端闭环；成本可按租户归集。

### 阶段二：平台化（补齐 P0-6 ~ P0-7 与 P1-1 ~ P1-5）

目标：从“内核”变成“可复用的能力平台”。

- 审批授权与多级审批、审批工作台
- 资产注册中心 + 发布状态机 + 管理控制台
- 编排层补齐、知识库工程链路
- 告警/SLO、评测闭环、SDK 与开放平台

出关标准：四类资产（模型/工具/Prompt/知识库）可注册、可灰度、可回滚；业务方可自助接入。

### 阶段三：运营化（补齐 P1-6 ~ P1-8 与 P2）

目标：从“平台”变成“可规模化运营的中台”。

- 工具市场与三方生态治理
- 计量计费与结算对账
- 容量调度、多区域、多模态
- 合规报表与审计证据包

出关标准：可对外承诺 SLA；可支撑多业务线独立结算；新增业务接入不需改内核代码。

---

## 6. 对外表述建议

**可以这样描述（准确且不夸大）：**

> react-ai 是面向企业级场景的 AI 智能体运行时与能力中台底座，提供多模型网关、工具沙箱与 HITL、分层记忆与向量检索、安全护栏、可观测性与统一 API，可作为 AI 中台的执行引擎层。配套身份网关、资产管控台、配额计量与多实例状态外置后，可演进为完整的企业级 AI 中台。

**避免这样描述：**

> “本项目就是企业级 AI 中台。”

原因：当前接入层无认证、无控制台、无配额计费、状态多为进程内实现，四项均属中台的硬性门槛。

---

## 附录：关键证据索引

| 能力 | 证据文件 |
|---|---|
| 运行时状态机 | [core/engine.py](file:///d:/code/react-ai/src/react_ai/core/engine.py) |
| 模型网关与容灾 | [llm/router.py](file:///d:/code/react-ai/src/react_ai/llm/router.py)、[llm/providers/](file:///d:/code/react-ai/src/react_ai/llm/providers) |
| 工具沙箱与韧性 | [tools/sandbox.py](file:///d:/code/react-ai/src/react_ai/tools/sandbox.py)、[tools/resilience.py](file:///d:/code/react-ai/src/react_ai/tools/resilience.py) |
| 幂等与审批 | [idempotency/](file:///d:/code/react-ai/src/react_ai/idempotency)、[approval/](file:///d:/code/react-ai/src/react_ai/approval) |
| 记忆与向量 | [memory/](file:///d:/code/react-ai/src/react_ai/memory)、[memory/vector/pgvector.py](file:///d:/code/react-ai/src/react_ai/memory/vector/pgvector.py) |
| 护栏 | [guardrails/](file:///d:/code/react-ai/src/react_ai/guardrails) |
| 可观测性 | [observability/](file:///d:/code/react-ai/src/react_ai/observability)、[api/main.py](file:///d:/code/react-ai/api/main.py) |
| 提示词版本与灰度 | [prompts/manager.py](file:///d:/code/react-ai/src/react_ai/prompts/manager.py) |
| 接入层现状 | [api/routes/agent.py](file:///d:/code/react-ai/api/routes/agent.py)、[api/main.py](file:///d:/code/react-ai/api/main.py) |
| 配置项 | [config/settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py) |
| 评测集 | [eval/](file:///d:/code/react-ai/eval) |
