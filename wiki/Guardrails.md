# Guardrails 安全合规护栏

模块：`react_ai.guardrails`。针对 LLM 应用输入/输出内容构筑安全防线，由总开关 `settings.security.enable_guardrails` 控制。

## 核心契约（guardrails/base.py）

文件：[guardrails/base.py](file:///d:/code/react-ai/src/react_ai/guardrails/base.py)

```python
class GuardrailDirection(Enum):  # INPUT / OUTPUT / BOTH —— 检查侧
class GuardrailSeverity(Enum):   # LOW(0) / MEDIUM(1) / HIGH(2) / CRITICAL(3)
class GuardrailAction(Enum):     # ALLOW / WARN / MODIFY / BLOCK / REDIRECT

@dataclass GuardrailViolation    # rule_id/rule_name/severity/action/description/matched_content/confidence
@dataclass GuardrailResult       # check_id/passed/action(取最严格)/violations/modified_content/fallback_content/latency_ms
@dataclass GuardrailContext      # request_id/session_id/user_id/direction 等检查上下文
```

`GuardrailResult.add_violation()` 自动将整体状态置为未通过，并按 ALLOW<WARN<MODIFY<REDIRECT<BLOCK 优先级取最严格动作。

## BaseGuardrail

子类公开接口与扩展点：

| 方法 | 说明 |
|---|---|
| `check(content, context=None) -> GuardrailResult` | 统一入口：方向过滤 (`_should_check`)、熔断器检查、pre/post 钩子、耗时统计与审计日志 |
| `_check_impl(content, context) -> GuardrailResult` | 子类实现的核心检测逻辑 |
| `register_rule(rule_id, rule_config)` / `unregister_rule` | 规则动态注册 |
| `add_pre_hook` / `add_post_hook` | 执行前后生命周期钩子 |
| `get_stats()` / `reset_stats()` | 命中统计 |
| `validate_input(text)` / `validate_output(text)` | 旧版异步协程兼容协议，返回 `GuardrailCheckResult(is_safe, flagged_reason, sanitized_text)` |

内置熔断器（Circuit Breaker）：护栏自身异常时 `_is_circuit_open()` 短路放行并告警，避免安全组件故障拖垮主链路。

## 内置护栏

### PromptInjectionGuardrail（输入侧阻断）

文件：[guardrails/injection.py](file:///d:/code/react-ai/src/react_ai/guardrails/injection.py)。`direction=INPUT`，`severity=HIGH`，`action=BLOCK`。正则模式库 `INJECTION_PATTERNS`（中英双语）：

```text
ignore (all )?(previous|above) instructions
system prompt override
you are now in developer mode
忽略(之前|以上)(所有)?(指示|命令)
```

### PIIMaskingGuardrail（双向脱敏）

文件：[guardrails/pii.py](file:///d:/code/react-ai/src/react_ai/guardrails/pii.py)。`direction=BOTH`，`action=MODIFY`——不阻断而是替换：

- 大陆手机号 `1[3-9]\d{9}` → 掩码
- 身份证号 `\d{17}[\dXx]` → `[ID_MASKED]`

脱敏后文本写入 `GuardrailResult.modified_content`。

### ModerationGuardrail（合规审核）

文件：[guardrails/moderation.py](file:///d:/code/react-ai/src/react_ai/guardrails/moderation.py)。`direction=BOTH`，构造参数 `blocklist: set[str] | None` 支持企业自定义违禁词。

### KeywordGuardrail（通用关键词）

`base.py` 内置的正则/关键词通用护栏，适合快速落地自定义规则而无需新建子类。

## GuardrailChain 组合链

```python
from react_ai.guardrails.base import GuardrailChain, GuardrailContext
from react_ai.guardrails.injection import PromptInjectionGuardrail
from react_ai.guardrails.pii import PIIMaskingGuardrail
from react_ai.guardrails.moderation import ModerationGuardrail

chain = GuardrailChain(name="prod_input_chain", short_circuit=True)  # 命中 BLOCK 即短路
chain.add_guardrail(PromptInjectionGuardrail())
chain.add_guardrail(PIIMaskingGuardrail())
chain.add_guardrail(ModerationGuardrail(blocklist={"内部代号X"}))

result = chain.check(user_input, GuardrailContext(session_id=sid))
if result.action == GuardrailAction.BLOCK:
    ...  # 拒绝并记录 result.violations
clean_text = result.modified_content or user_input
```

链级接口：`add_guardrail(guardrail, position=None)`、`remove_guardrail`、`get_guardrails()`、`clear()`、`check()`。

## 接入建议

护栏目前需在业务层显式调用（API 路由/网关中间件中前置 `chain.check()`，模型输出后置再次 `check(direction=OUTPUT)`），尚未自动织入 `ReActAgent` 循环。
