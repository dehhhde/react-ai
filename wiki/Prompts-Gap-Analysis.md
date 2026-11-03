# 提示词模块企业级差距分析

> 适用对象：[src/react_ai/prompts/](file:///d:/code/react-ai/src/react_ai/prompts/) 模块及其在项目中的实际使用情况。
> 本文档回答：**当前 prompts 模块到了什么水准？距离企业级生产提示词工程还差什么、怎么补？**
> 更新时间：2026-11-02

---

## 1. 当前架构速览

```text
                       ┌─────────────────────────────┐
   业务代码 ──硬编码──► │  SLOT_EXTRACTION_PROMPT 等   │  ← examples/aftersales_langgraph.py
   (system prompt)      │  裸字符串，直接拼进 messages │
                       └─────────────────────────────┘

                       ┌─────────────────────────────┐
   PromptManager  ────► │  模板字符串 (Jinja2 render)  │  ← 仅 react_system 一条
   (内存 dict)          │  BasePrompt 实体 (简单 {{}}) │  ← 仅 ReActPrompt 一个
                       └─────────────────────────────┘
                                │
                                ▼  ⚠️ 核心引擎 ReActAgent 完全未使用 prompts 模块
                       ┌─────────────────────────────┐
   ReActAgent._loop    │  直接把 query 当 USER 消息    │  ← 无 system prompt 注入
                       │  调 llm.generate(messages)   │     AgentConfig.system_prompt_override
                       └─────────────────────────────┘     字段存在但从未被读取
```

### 已实现能力清单

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 基类抽象 | [prompts/base.py](file:///d:/code/react-ai/src/react_ai/prompts/base.py) `BasePrompt` | name/version/description/tags/output_format/output_schema/required_variables/optional_variables 字段齐全 |
| 变量存在性校验 | `BasePrompt.validate_variables` | 必填变量缺失或为 None 时抛 `ValueError` |
| 模板渲染（实体模式） | `BasePrompt._render_template` | 简单 `{{key}}` 字符串替换，不支持条件/循环/过滤器 |
| 模板渲染（字符串模式） | `PromptManager.render` | Jinja2 `Template.render`，支持完整 Jinja 语法 |
| 少样本注入 | `BasePrompt.render` 步骤 4 | `examples` 列表依次注入为 user/assistant 消息对 |
| 渲染审计 | `PromptResult` | trace_id、version、variables_used、render_duration_ms |
| 内置模板 | [prompts/templates/react.py](file:///d:/code/react-ai/src/react_ai/prompts/templates/react.py) | `REACT_SYSTEM_PROMPT` + `ReActPrompt` 实体 |
| 消息协议转换 | `PromptMessage.to_chat_message` / `PromptResult.to_chat_messages` | 与项目 `ChatMessage` 模型打通 |

**结论：骨架字段齐全、接口干净，但属于"演示级完整"——核心引擎不接、渲染路径分裂、安全与版本能力缺失，按生产标准衡量存在下述 12 项差距。**

---

## 2. 企业级差距分析（按重要性排序）

### 2.1 与核心引擎完全脱节 —— prompts 模块是孤岛 ⚠️ P0

**现状**：`ReActAgent.stream()`（[engine.py#L92-L103](file:///d:/code/react-ai/src/react_ai/core/engine.py#L92-L103)）只把用户 query 作为 `MessageRole.USER` 追加到 `state.messages`，然后直接调 `self.llm.generate(messages=state.messages, ...)`，**没有任何 system prompt 注入**。`AgentConfig.system_prompt_override` 字段（[schemas/agent.py#L15](file:///d:/code/react-ai/src/react_ai/schemas/agent.py#L15)）存在但全项目无任何读取点。业务侧 `aftersales_langgraph.py` 直接硬编码 `SLOT_EXTRACTION_PROMPT = """..."""` 后 `ChatMessage(role="system", content=SLOT_EXTRACTION_PROMPT)` 拼消息。

**风险**：
1. prompts 模块形同虚设，企业沉淀的提示词规范（ReAct 闭环约束、安全边界）无法在核心执行链生效；
2. 业务提示词散落各处，改一处漏一处，无法统一审计与灰度；
3. `system_prompt_override` 是死字段，运维/业务方无法通过配置覆盖提示词。

**企业级做法**：在 `ReActAgent` 初始化或 `_loop` 入口，通过 `PromptManager.render_prompt()` 注入 system 消息，并支持 `system_prompt_override` 覆盖：

```python
# engine.py 初始化时
from react_ai.prompts.manager import prompt_manager

def _build_system_messages(self) -> list[ChatMessage]:
    if self.config.system_prompt_override:
        return [ChatMessage(role=MessageRole.SYSTEM, content=self.config.system_prompt_override)]
    result = prompt_manager.render_prompt("react_standard", variables={}, trace_id=self._ctx.run_id)
    return result.to_chat_messages()

# stream() 中，state.messages 首条为 system
state.messages = self._build_system_messages() + [ChatMessage(role=MessageRole.USER, content=query)]
```

业务侧 `aftersales_langgraph.py` 的 `SLOT_EXTRACTION_PROMPT` 迁移为 `BasePrompt` 子类并注册到 `prompt_manager`，由统一入口渲染与审计。

**验收标准**：核心引擎执行时 `messages[0].role == system` 且内容来自 prompts 模块；`system_prompt_override` 可覆盖默认 system 提示词；grep 全项目无裸 `"""你是..."""` 硬编码系统提示词（examples 除外且须有迁移计划）。

---

### 2.2 渲染路径不一致 —— 同一模块两套引擎 ⚠️ P0

**现状**：`BasePrompt._render_template` 用 `str.replace("{{key}}", str(value))`（[base.py#L86-L93](file:///d:/code/react-ai/src/react_ai/prompts/base.py#L86-L93)），不支持条件、循环、过滤器、默认值；而 `PromptManager.render` 用 Jinja2 `Template(raw).render(**variables)`（[manager.py#L33-L38](file:///d:/code/react-ai/src/react_ai/prompts/manager.py#L33-L38)）。两条路径并存，行为不一致。

**风险**：
1. 开发者不知道该用哪条路径，实体模式（`render_prompt`）能力弱、字符串模式（`render`）缺审计；
2. 简单替换无默认值机制，变量缺失只能抛错，无法优雅降级（如 `{{ company | default("本平台") }}`）；
3. 无转义控制，变量含特殊字符时可能破坏提示词结构。

**企业级做法**：统一到 Jinja2 单一路径。`BasePrompt._render_template` 改为内部持 `jinja2.Template`（懒编译缓存），变量支持默认值、过滤器、长度截断：

```python
from jinja2 import Template, StrictUndefined

class BasePrompt(ABC):
    def __init__(self):
        ...
        self._compiled: dict[str, Template] = {}

    def _render_template(self, template: str, variables: dict) -> str:
        tpl = self._compiled.setdefault(template, Template(template, undefined=StrictUndefined))
        return tpl.render(**variables)   # 缺变量抛 UndefinedError，由 render() 捕获转业务异常
```

同时提供 `truncate`、`mask_pii` 等自定义过滤器，配合护栏使用。

**验收标准**：`BasePrompt` 与 `PromptManager` 使用同一 Jinja2 渲染内核；模板可写 `{{ order_id | default("未提供") }}`、`{{ content | truncate(200) }}`；实体模式与字符串模式渲染同一模板输出一致。

---

### 2.3 输出约束字段形同虚设 —— output_schema 未生效 ⚠️ P0

**现状**：`BasePrompt` 声明了 `output_format`（`json`/`structured`/`free_text`）与 `output_schema`（JSON Schema）字段，但 `render()` 从头到尾没有读取它们——既没有把 schema 注入 system prompt，也没有和 Pydantic / function calling 绑定。`output_schema` 只是一个永远不被消费的元数据。

**风险**：声明的结构化输出约束完全无效，模型可能自由发挥输出非 JSON / 非法字段，下游 Pydantic 校验失败率高。

**企业级做法**：`render()` 根据 `output_format` 自动拼接输出约束段，并支持与协议层 function calling 联动：

```python
def render(self, variables, trace_id=None):
    system_content = self._render_template(self.system_template, variables)
    if self.output_format == "json" and self.output_schema:
        system_content += f"\n\n输出要求：严格按以下 JSON Schema 输出，禁止输出任何解释文字：\n{json.dumps(self.output_schema, ensure_ascii=False)}"
    ...
```

更优方案：将 `output_schema` 直接映射为 `ToolDefinition`，通过 `tool_choice` 强制调用（项目已有 `SLOT_TOOL` 先例），由协议层保证结构合法。

**验收标准**：`output_format="json"` 的 prompt 渲染后 system 消息末尾包含 schema 约束；或 `output_schema` 自动生成 `ToolDefinition` 供 LLM 调用；字段不再是死字段。

---

### 2.4 安全护栏未集成 —— 变量直入模板无防护 🔒 P1

**现状**：`BasePrompt._render_template` 直接 `str(value)` 替换进模板，没有任何清洗。项目已有完整的 `GuardrailChain`（[guardrails/base.py](file:///d:/code/react-ai/src/react_ai/guardrails/base.py)）含 `PromptInjectionGuardrail` 与 `PIIMaskingGuardrail`，但 prompts 模块完全不调用。用户/外部变量可能包含注入指令（如"忽略上述所有指示"），直接拼入 system prompt 会污染整个提示词。

**风险**：提示词注入攻击面；PII（手机号、身份证）可能通过变量进入 system prompt 并被日志/审计记录。

**企业级做法**：在 `render()` 的变量注入阶段接入护栏链，区分"可信内部变量"与"用户输入变量"：

```python
def render(self, variables, trace_id=None, guardrail_chain=None):
    if guardrail_chain is not None:
        for key in self.user_input_variables:   # 声明哪些变量来自用户
            ctx = GuardrailContext(request_id=trace_id, direction=GuardrailDirection.INPUT)
            res = guardrail_chain.check(str(variables[key]), ctx)
            if res.action == GuardrailAction.BLOCK:
                raise PromptInjectionError(...)
            variables[key] = res.modified_content or variables[key]  # PII 脱敏后注入
    ...
```

参考 `aftersales_langgraph.py` 已落地的双轨原则：**发往 LLM 的内容用脱敏后文本，业务校验与存储用原始输入**。

**验收标准**：变量注入前经过 `PromptInjectionGuardrail` + `PIIMaskingGuardrail`；注入命中时阻断渲染（不写入审计日志的原文）；PII 变量在渲染结果中为脱敏形态。

---

### 2.5 版本管理无持久化、无灰度 —— 进程重启即丢失 📦 P1

**现状**：`PromptManager` 用内存 `dict` 存储模板与实体，`version` 字段仅作展示。没有版本注册中心、没有 A/B 测试路由、没有回滚能力、没有持久化。`prompt_manager` 是模块级单例，每次启动都是默认的两条内置项。

**风险**：
1. 提示词迭代无法灰度（必须全量替换，出问题影响全部流量）；
2. 无法回滚到上一版本；
3. 多实例部署时各实例提示词版本可能不一致（如果有动态注册逻辑）；
4. 无法追溯"某次 LLM 响应用的是哪版提示词"。

**企业级做法**：
1. 引入版本化注册表：`register_prompt(prompt, version=...)` 支持同名多版本并存，`get_prompt(name, version=None)` 可按版本取；
2. 持久化到数据库或配置中心（PG 表 / 配置服务），启动时加载，支持热更新；
3. 灰度路由：按 `trace_id` 哈希、用户标签、百分比分流到不同版本；
4. 回滚：一键切换活跃版本。

```python
class PromptManager:
    def register_prompt(self, prompt: BasePrompt, version: str | None = None):
        ver = version or prompt.version
        self._prompts.setdefault(prompt.name, {})[ver] = prompt

    def get_prompt(self, name: str, version: str | None = None) -> BasePrompt | None:
        versions = self._prompts.get(name, {})
        return versions.get(version) or versions.get(self._active_version.get(name))
```

**验收标准**：同名 prompt 可注册多版本并存；可按版本号取回；支持配置驱动的灰度分流比例；持久化后重启不丢失；`PromptResult.version` 与实际渲染版本一致。

---

### 2.6 变量校验太浅 —— 只查存在性 🔍 P1

**现状**：`validate_variables` 只检查必填变量"存在且非 None"（[base.py#L77-L84](file:///d:/code/react-ai/src/react_ai/prompts/base.py#L77-L84)）。没有类型校验、长度限制、正则格式约束；未在 `required_variables` / `optional_variables` 中声明的变量被静默忽略（不报错也不警告），容易掩盖拼写错误（如 `orderId` 写成 `order_id` 不会被发现）。

**风险**：非法变量值（超长文本、错误类型）进入模板导致提示词污染或下游解析失败；变量名拼写错误无感知。

**企业级做法**：为变量声明 schema（类型、长度、正则、默认值），渲染前做完整校验，未声明变量在 debug 模式下告警：

```python
variable_schemas: ClassVar[dict[str, VariableSchema]] = {
    "order_id": VariableSchema(type=str, pattern=r"^[A-Za-z0-9_\-]{4,64}$", required=True),
    "user_question": VariableSchema(type=str, max_length=2000, required=True),
    "chat_history": VariableSchema(type=list, required=False, default=[]),
}
```

**验收标准**：变量类型/长度/格式不合规时渲染抛明确异常；未声明变量在开发模式下告警日志；提供 `VariableSchema` 数据类。

---

### 2.7 可观测性薄弱 —— 只有一行日志 📊 P1

**现状**：`render()` 仅 `logger.info("prompt_rendered ...")` 一行（[base.py#L125-L128](file:///d:/code/react-ai/src/react_ai/prompts/base.py#L125-L128)）。项目已有 `observability/tracer.py`（OpenTelemetry span）、`observability/metrics.py`、`utils/token_counter.py`，但 prompts 模块均未接入。无法在链路追踪中看到 prompt 渲染 span，无法统计 token 消耗，无法把 prompt 版本与 LLM 响应质量关联。

**风险**：线上问题（如某版提示词导致 token 暴涨、响应质量下降）无法快速定位归因。

**企业级做法**：
1. `render()` 内用 `tracer.start_span("prompt.render", attributes={"prompt.name", "prompt.version", "trace_id"})` 包 span；
2. 渲染后调用 `estimate_messages_tokens(result.messages)` 并写入 `PromptResult.token_count` + metrics；
3. `PromptResult` 持久化到审计存储（PG / 对象存储），可按 trace_id 回查"这次请求用了哪版提示词、注入了哪些变量快照"。

**验收标准**：链路追踪中可见 `prompt.render` span 且带 name/version 属性；`PromptResult` 含 `token_count`；可按 trace_id 查询历史渲染快照（变量值需脱敏后存储）。

---

### 2.8 少样本静态写死 —— 无法动态选择 🎯 P2

**现状**：`examples` 是类属性，写死在代码里（[test_prompts.py#L40-L50](file:///d:/code/react-ai/tests/unit/test_prompts.py#L40-L50) 即典型用法）。所有用户请求都看到完全相同的示例，无法根据输入语义挑选最相关示例（dynamic few-shot），也没有数量上限与 token 预算截断。

**风险**：示例与当前问题不相关时反而干扰模型；示例过多挤占上下文窗口。

**企业级做法**：
1. 示例外置到文件/数据库，支持按 prompt 版本管理；
2. 引入动态示例选择：基于输入 embedding 检索 top-k 最相关示例（复用项目 `tools/builtin/retrieval.py` 的检索能力）；
3. 按 token 预算截断示例数量。

**验收标准**：示例可外置配置；相同 prompt 对不同输入返回不同 top-k 示例；示例总 token 不超过设定预算。

---

### 2.9 无多语言 / 本地化 🌐 P2

**现状**：所有模板硬编码中文，没有按 locale 选择模板的能力。跨境业务或多语言客服场景无法支持。

**企业级做法**：`BasePrompt` 增加 `locale` 维度，`system_template`/`user_template` 按 locale 返回不同文本，或模板文件按 `templates/{locale}/{name}.jinja` 组织。

**验收标准**：同一 prompt 名称可按 `locale="en_US"` / `zh_CN` 渲染出不同语言内容；缺失 locale 时有 fallback 规则。

---

### 2.10 无外部存储与热更新 🔥 P2

**现状**：模板只能通过代码 `register()` / `register_prompt()` 注册，没有从文件、数据库、配置中心加载的能力；没有热加载，改提示词必须重启服务。

**企业级做法**：
1. 支持从 `prompts/` 目录加载 `.jinja` / `.yaml` 模板文件（带元数据 front-matter）；
2. 支持从配置中心（如 Apollo、Nacos）或 PG 加载，监听变更热更新；
3. 文件系统 watcher 自动重载。

**验收标准**：新增/修改模板文件无需重启即可生效；从 PG/配置中心加载的模板变更有热更新机制。

---

### 2.11 无评测集与回归机制 🧪 P2

**现状**：没有 prompt 级别的评测集与回归脚本。改一版提示词无法量化"哪类用例退步了"。这与 [Intent-Recognition-Optimization.md](file:///d:/code/react-ai/wiki/Intent-Recognition-Optimization.md) §2.1 提到的评测集基础设施是同一类缺口，应统一建设。

**企业级做法**：建立 `eval/prompts/` 目录，每个 prompt 配一份样本集（输入变量 + 期望输出/期望命中字段），`eval/run_prompt_eval.py` 跑回归并输出准确率 diff，接入 CI。

**验收标准**：每个核心 prompt 有 ≥20 条评测样本；提示词变更 PR 必须附评测分数 diff。

---

### 2.12 无渲染缓存 ⚡ P2

**现状**：每次 `render()` 都重新编译模板、重新替换。`PromptManager` 无 Jinja2 `Environment` 缓存（每次 `Template(raw)` 新建）。高并发下模板编译开销浪费。

**企业级做法**：`PromptManager` 持全局 `jinja2.Environment`，模板按名称缓存编译结果；相同变量的渲染结果可按内容哈希缓存（注意 PII 变量不缓存原文）。

**验收标准**：同名模板第二次渲染复用编译产物；提供缓存命中率指标。

---

## 3. 实施优先级路线

| 级别 | 事项 | 排序依据 |
|---|---|---|
| **P0（阻塞上线）** | 2.1 引擎接入 prompts → 2.2 统一 Jinja2 渲染 → 2.3 output_schema 生效 | 不做则 prompts 模块白建、核心 Agent 无系统提示词约束、输出结构无保障 |
| **P1（安全合规 / 可运维）** | 2.4 护栏集成 → 2.5 版本持久化+灰度 → 2.6 变量 schema 校验 → 2.7 可观测性 | 注入/PII 是合规红线；无版本灰度无法安全迭代；无观测无法排障 |
| **P2（质量 / 效率）** | 2.8 动态少样本 → 2.9 多语言 → 2.10 外部存储+热更新 → 2.11 评测回归 → 2.12 渲染缓存 | 体验与效率提升，依赖 P0/P1 落地后再做收益更大 |

> 配套建设（与 P1 并行）：**提示词审计存储**——每次渲染的 `PromptResult`（脱敏后变量快照）落库，这是 2.5 版本追溯、2.7 可观测性、2.11 评测回归的共同基础设施，建议与 PG Checkpointer 复用同一数据库。

---

## 4. 相关代码与文档索引

- 提示词模块：[prompts/base.py](file:///d:/code/react-ai/src/react_ai/prompts/base.py) · [prompts/manager.py](file:///d:/code/react-ai/src/react_ai/prompts/manager.py) · [prompts/templates/react.py](file:///d:/code/react-ai/src/react_ai/prompts/templates/react.py)
- 提示词 Wiki：[Prompts.md](file:///d:/code/react-ai/wiki/Prompts.md)
- 核心引擎（接入点）：[core/engine.py](file:///d:/code/react-ai/src/react_ai/core/engine.py) `ReActAgent.stream/_loop` · [schemas/agent.py](file:///d:/code/react-ai/src/react_ai/schemas/agent.py) `AgentConfig.system_prompt_override`
- 护栏组件（待集成）：[guardrails/base.py](file:///d:/code/react-ai/src/react_ai/guardrails/base.py) `GuardrailChain` · [guardrails/injection.py](file:///d:/code/react-ai/src/react_ai/guardrails/injection.py) · [guardrails/pii.py](file:///d:/code/react-ai/src/react_ai/guardrails/pii.py)
- 可观测性（待接入）：[observability/tracer.py](file:///d:/code/react-ai/src/react_ai/observability/tracer.py) · [observability/metrics.py](file:///d:/code/react-ai/src/react_ai/observability/metrics.py) · [utils/token_counter.py](file:///d:/code/react-ai/src/react_ai/utils/token_counter.py)
- 业务硬编码提示词（待迁移）：[examples/aftersales_langgraph.py](file:///d:/code/react-ai/examples/aftersales_langgraph.py) `SLOT_EXTRACTION_PROMPT`
- 参考文档：[Intent-Recognition.md](file:///d:/code/react-ai/wiki/Intent-Recognition.md)（差距分析格式参照） · [Guardrails.md](file:///d:/code/react-ai/wiki/Guardrails.md) · [LLM-Gateway.md](file:///d:/code/react-ai/wiki/LLM-Gateway.md)
