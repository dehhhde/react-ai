"""统一提示词基类、消息协议与渲染契约.

企业级能力（对应 wiki/Prompts-Gap-Analysis.md）：
- 2.2 统一 Jinja2 渲染内核：与 PromptManager 共用 env.render_template（StrictUndefined）
- 2.3 output_schema 生效：output_format=="json" 时自动追加 JSON Schema 约束段
- 2.4 护栏集成：user_input_variables 声明的变量渲染前过 GuardrailChain
- 2.6 变量 Schema：apply_schemas 做类型/长度/正则校验并补默认值
- 2.7 可观测性：tracer.span + metrics.record_prompt_render + prompt_audit_store 审计
- 2.8 动态少样本：examples_source 外置 JSON + 词面相关性排序 + token 预算截断
- 2.9 多语言：localized_templates 按 locale 选择 system/user 模板
- 2.12 渲染缓存：env 层按模板源码哈希缓存编译产物
- 持久化：to_spec()/from_spec() 序列化重建 DynamicPrompt（版本存储与热更新基础）
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from jinja2 import UndefinedError

from react_ai.prompts.audit import build_audit_entry, prompt_audit_store
from react_ai.prompts.env import render_template
from react_ai.prompts.schema import VariableSchema, apply_schemas
from react_ai.utils.token_counter import estimate_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)


class PromptInjectionError(ValueError):
    """用户输入变量命中提示词注入护栏，渲染被阻断."""


@dataclass
class PromptMessage:
    role: str  # system / user / assistant
    content: str

    def to_chat_message(self) -> Any:
        """转换为 react-ai 统一协议的 ChatMessage 模型."""
        from react_ai.schemas.messages import ChatMessage, MessageRole

        return ChatMessage(role=MessageRole(self.role), content=self.content)


@dataclass
class PromptResult:
    messages: list[PromptMessage]  # 渲染后的完整消息列表
    prompt_name: str               # 使用的 Prompt 名称
    version: str                   # 使用的版本
    trace_id: str                  # 链路追踪ID
    variables_used: dict[str, Any] # 实际注入的变量快照（护栏脱敏后），用于审计
    render_duration_ms: int        # 渲染耗时
    token_count: int = 0           # 渲染产物 token 估算值

    def to_chat_messages(self) -> list[Any]:
        """将渲染结果中的所有消息转换为 ChatMessage 列表."""
        return [msg.to_chat_message() for msg in self.messages]


def _tokenize(text: str) -> set[str]:
    """中英混合分词的轻量实现：英文/数字词 + 中文单字与相邻双字组合."""
    words = set(re.findall(r"[a-zA-Z0-9]+", text.lower()))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    words.update(cjk)
    words.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return words


def _lexical_overlap(query: str, candidate: str) -> float:
    """Jaccard 词面重叠度，作为少样本相关性分数."""
    a, b = _tokenize(query), _tokenize(candidate)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class BasePrompt(ABC):
    """Prompt 基类：统一模板、变量校验、护栏、渲染、输出约束和审计。"""

    name: str = ""                     # Prompt 唯一标识，如 "after_sales_router"
    version: str = "1.0.0"             # 版本号，便于 A/B 测试和回滚
    description: str = ""              # 用途描述
    tags: list[str] = field(default_factory=list)  # 标签，如 ["router", "after_sales"]

    # 输出约束：告诉模型应该以什么格式返回
    output_format: str = ""            # 例如 "json"、"structured"、"free_text"
    output_schema: dict | None = None  # JSON Schema，用于结构化输出校验

    # 变量声明
    required_variables: list[str] = field(default_factory=list)   # 必填变量，如 ["order_id", "user_question"]
    optional_variables: list[str] = field(default_factory=list)   # 可选变量，如 ["chat_history", "policy_docs"]
    variable_schemas: dict[str, VariableSchema] = field(default_factory=dict)  # 变量类型/长度/正则约束（2.6）

    # 不可信输入声明：这些变量的值来自用户，渲染前必须过护栏链（2.4）
    user_input_variables: list[str] = field(default_factory=list)

    # 多语言模板（2.9）: {"en_US": {"system": ..., "user": ...}}
    localized_templates: dict[str, dict[str, str]] = field(default_factory=dict)

    # 少样本外置与动态选取（2.8）
    examples_source: str = ""          # 外置示例 JSON 文件路径（支持热更新，按 mtime 重载）
    dynamic_examples: bool = False     # 是否按与用户输入的相关性动态挑选示例
    max_examples: int = 4              # 单次渲染注入的示例数量上限
    examples_token_budget: int = 1500  # 示例总 token 预算

    # 默认护栏链与示例文件缓存（类级共享）
    _default_chain: Any = None
    _chain_lock: threading.Lock = threading.Lock()
    _examples_file_cache: dict[str, tuple[float, list[dict]]] = {}

    def __init__(self):
        for attr in ("tags", "required_variables", "optional_variables", "user_input_variables"):
            if not isinstance(getattr(self, attr, None), list):
                setattr(self, attr, [])
        for attr in ("variable_schemas", "localized_templates"):
            if not isinstance(getattr(self, attr, None), dict):
                setattr(self, attr, {})

    @property
    @abstractmethod
    def system_template(self) -> str:
        """系统提示模板，定义角色、职责、边界、输出格式。"""

    @property
    def user_template(self) -> str:
        """用户提示模板，通常是当前轮次的用户输入 + 上下文。

        返回空字符串表示纯 system 提示词（不追加 user 消息）。
        """
        return "{{user_input}}"

    @property
    def examples(self) -> list[dict]:
        """少样本示例，格式为 [{"input": ..., "output": ...}, ...]"""
        return []

    def validate_variables(self, variables: dict) -> list[str]:
        """校验变量是否齐全，返回缺失的变量名列表。"""
        missing = []
        req_vars = self.required_variables if isinstance(self.required_variables, (list, tuple, set)) else []
        for var in req_vars:
            if var not in variables or variables[var] is None:
                missing.append(var)
        return missing

    # ------------------------------------------------------------------
    # 内部渲染步骤
    # ------------------------------------------------------------------

    def _select_templates(self, locale: str | None) -> tuple[str, str]:
        """按 locale 选择 (system, user) 模板（2.9）：精确 → 主语言 → 默认."""
        if locale and self.localized_templates:
            primary = re.split(r"[-_]", locale)[0].lower()
            for key, tpl in self.localized_templates.items():
                if key.lower() == locale.lower():
                    return tpl.get("system", self.system_template), tpl.get("user", self.user_template)
            for key, tpl in self.localized_templates.items():
                if re.split(r"[-_]", key)[0].lower() == primary:
                    return tpl.get("system", self.system_template), tpl.get("user", self.user_template)
        return self.system_template, self.user_template

    @classmethod
    def _get_default_guardrail_chain(cls):
        """懒建默认输入护栏链：注入检测（BLOCK 短路）+ PII 脱敏（MODIFY）."""
        if cls._default_chain is None:
            with cls._chain_lock:
                if cls._default_chain is None:
                    from react_ai.guardrails.base import GuardrailChain
                    from react_ai.guardrails.injection import PromptInjectionGuardrail
                    from react_ai.guardrails.pii import PIIMaskingGuardrail

                    chain = GuardrailChain(name="prompt_input_chain", short_circuit=True)
                    chain.add_guardrail(PromptInjectionGuardrail())
                    chain.add_guardrail(PIIMaskingGuardrail())
                    cls._default_chain = chain
        return cls._default_chain

    def _apply_input_guardrails(
        self,
        variables: dict[str, Any],
        trace_id: str,
        guardrail_chain: Any,
    ) -> tuple[dict[str, Any], list[str]]:
        """对声明的用户输入变量执行护栏链检查（2.4）.

        - BLOCK → 抛 PromptInjectionError；
        - MODIFY → 用脱敏后的内容替换变量值（审计快照同步存脱敏值）；
        - settings.security.enable_guardrails 为总开关。
        """
        from react_ai.config.settings import settings

        if not settings.security.enable_guardrails or not self.user_input_variables:
            return variables, []

        from react_ai.guardrails.base import (
            GuardrailAction,
            GuardrailContext,
            GuardrailDirection,
        )

        chain = guardrail_chain or self._get_default_guardrail_chain()
        merged = dict(variables)
        hits: list[str] = []
        for key in self.user_input_variables:
            value = merged.get(key)
            if value is None:
                continue
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            res = chain.check(
                text,
                GuardrailContext(direction=GuardrailDirection.INPUT, request_id=trace_id),
            )
            if res.action == GuardrailAction.BLOCK:
                reasons = "; ".join(v.description for v in res.violations if v.description) or "命中注入检测规则"
                raise PromptInjectionError(
                    f"Prompt '{self.name}' 变量 '{key}' 被输入护栏阻断: {reasons}"
                )
            if res.action == GuardrailAction.MODIFY and res.modified_content is not None:
                merged[key] = res.modified_content
            if not res.passed:
                hits.extend(
                    f"{v.rule_id or v.rule_name}:{v.action.value}" for v in res.violations
                )
        return merged, hits

    def _load_file_examples(self) -> list[dict]:
        """加载 examples_source 外置示例（按 mtime 缓存，文件更新即重载，2.8/2.10）."""
        if not self.examples_source:
            return []
        path = self.examples_source
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            logger.warning("examples_source 文件不存在: %s", path)
            return []
        cached = BasePrompt._examples_file_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            examples = data if isinstance(data, list) else data.get("examples", [])
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("examples_source 加载失败 path=%s error=%s", path, exc)
            return []
        BasePrompt._examples_file_cache[path] = (mtime, examples)
        return examples

    def _extract_query(self, variables: dict[str, Any]) -> str:
        """提取用于示例相关性打分的查询文本."""
        keys = self.user_input_variables or (["user_input"] if "user_input" in variables else [])
        parts = [str(variables[k]) for k in keys if variables.get(k) is not None]
        return " ".join(parts)

    def _select_examples(self, variables: dict[str, Any]) -> list[dict]:
        """少样本选取（2.8）：静态全量 或 动态按相关性排序 + 数量/token 双预算截断."""
        pool = list(self.examples) + self._load_file_examples()
        if not pool:
            return []
        if not self.dynamic_examples:
            return pool[: self.max_examples] if self.max_examples > 0 else pool

        query = self._extract_query(variables)
        scored = sorted(
            pool,
            key=lambda ex: _lexical_overlap(query, f"{ex.get('input', '')} {ex.get('output', '')}"),
            reverse=True,
        )
        selected: list[dict] = []
        budget = self.examples_token_budget
        for ex in scored[: self.max_examples if self.max_examples > 0 else len(scored)]:
            cost = estimate_tokens(str(ex.get("input", ""))) + estimate_tokens(str(ex.get("output", "")))
            if selected and budget > 0 and cost > budget:
                break
            selected.append(ex)
            budget -= cost
        return selected

    def _schema_constraint_block(self) -> str:
        """output_schema 生效（2.3）：把 Schema 编译为强约束指令追加到 system 尾部."""
        schema_text = json.dumps(self.output_schema, ensure_ascii=False, indent=2)
        return (
            "\n\n## 输出格式约束（必须严格遵守）\n"
            "仅输出一个 JSON 对象，不得包含任何解释、前后缀或 Markdown 代码块标记。"
            "JSON 必须满足以下 Schema：\n" + schema_text
        )

    # ------------------------------------------------------------------
    # 渲染主流程
    # ------------------------------------------------------------------

    def render(
        self,
        variables: dict,
        trace_id: str | None = None,
        guardrail_chain: Any = None,
        locale: str | None = None,
    ) -> PromptResult:
        """渲染 Prompt：Schema 校验 → 护栏 → Jinja 渲染 → 示例注入 → 观测审计."""
        trace_id = trace_id or uuid.uuid4().hex
        start = time.time()

        from react_ai.observability.metrics import metrics
        from react_ai.observability.tracer import tracer

        with tracer.span(
            "prompt.render",
            {"prompt.name": self.name, "prompt.version": self.version, "trace_id": trace_id},
        ):
            # 1. 多语言模板选择（2.9）
            system_src, user_src = self._select_templates(locale)

            # 2. 必填变量校验（保持既有错误契约）
            missing = self.validate_variables(variables)
            if missing:
                raise ValueError(f"Prompt '{self.name}' 缺少必填变量: {missing}")

            # 3. 变量 Schema 校验与默认值补齐（2.6）
            declared = (
                set(self.required_variables)
                | set(self.optional_variables)
                | set(self.variable_schemas)
                | {"user_input"}
            )
            merged = apply_schemas(self.variable_schemas, variables, declared)
            undeclared = set(merged) - declared
            if undeclared:
                logger.warning(
                    "Prompt '%s' 收到未声明变量: %s", self.name, sorted(undeclared)
                )

            # 4. 用户输入护栏（2.4）
            merged, guardrail_hits = self._apply_input_guardrails(merged, trace_id, guardrail_chain)

            # 5. Jinja2 渲染（2.2/2.12），未定义变量转业务异常
            try:
                system_content = render_template(system_src, merged)
                user_content = render_template(user_src, merged) if user_src else ""
            except UndefinedError as exc:
                raise ValueError(f"Prompt '{self.name}' 模板变量未定义: {exc.message}") from exc

            # 6. 结构化输出约束（2.3）
            if self.output_format == "json" and self.output_schema:
                system_content += self._schema_constraint_block()

            # 7. 消息组装 + 少样本注入
            messages: list[PromptMessage] = [PromptMessage(role="system", content=system_content)]
            for example in self._select_examples(merged):
                messages.append(PromptMessage(role="user", content=str(example.get("input", ""))))
                messages.append(PromptMessage(role="assistant", content=str(example.get("output", ""))))
            if user_src:
                messages.append(PromptMessage(role="user", content=user_content))

            duration = int((time.time() - start) * 1000)
            token_count = estimate_messages_tokens(messages)

            result = PromptResult(
                messages=messages,
                prompt_name=self.name,
                version=self.version,
                trace_id=trace_id,
                variables_used=merged,
                render_duration_ms=duration,
                token_count=token_count,
            )

        # 8. 指标与审计（2.7）
        metrics.record_prompt_render(self.name, self.version, token_count, float(duration))
        prompt_audit_store.record(build_audit_entry(result, locale=locale, guardrail_hits=guardrail_hits))
        logger.info(
            "prompt_rendered name=%s version=%s trace=%s duration_ms=%d tokens=%d",
            self.name, self.version, trace_id, duration, token_count,
        )
        return result

    # ------------------------------------------------------------------
    # 序列化（版本存储/热更新基础，2.5/2.10）
    # ------------------------------------------------------------------

    def to_spec(self) -> dict[str, Any]:
        """导出完整可重建的 Prompt spec（JSON 安全）."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tags": list(self.tags),
            "output_format": self.output_format,
            "output_schema": self.output_schema,
            "required_variables": list(self.required_variables),
            "optional_variables": list(self.optional_variables),
            "user_input_variables": list(self.user_input_variables),
            "variable_schemas": {k: v.to_dict() for k, v in self.variable_schemas.items()},
            "system_template": self.system_template,
            "user_template": self.user_template,
            "examples": list(self.examples),
            "localized_templates": {k: dict(v) for k, v in self.localized_templates.items()},
            "examples_source": self.examples_source,
            "dynamic_examples": self.dynamic_examples,
            "max_examples": self.max_examples,
            "examples_token_budget": self.examples_token_budget,
        }

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "DynamicPrompt":
        """从 spec 重建提示词实体."""
        return DynamicPrompt(spec)


class DynamicPrompt(BasePrompt):
    """由 spec 字典重建的提示词实体（用于持久化加载与热更新）."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__()
        self.name = spec.get("name", "")
        self.version = spec.get("version", "1.0.0")
        self.description = spec.get("description", "")
        self.tags = list(spec.get("tags", []))
        self.output_format = spec.get("output_format", "")
        self.output_schema = spec.get("output_schema")
        self.required_variables = list(spec.get("required_variables", []))
        self.optional_variables = list(spec.get("optional_variables", []))
        self.user_input_variables = list(spec.get("user_input_variables", []))
        self.variable_schemas = {
            k: VariableSchema.from_dict(v)
            for k, v in (spec.get("variable_schemas") or {}).items()
        }
        self.localized_templates = {
            k: dict(v) for k, v in (spec.get("localized_templates") or {}).items()
        }
        self.examples_source = spec.get("examples_source", "")
        self.dynamic_examples = bool(spec.get("dynamic_examples", False))
        self.max_examples = int(spec.get("max_examples", 4))
        self.examples_token_budget = int(spec.get("examples_token_budget", 1500))
        self._system = spec.get("system_template", "")
        self._user = spec.get("user_template", "{{user_input}}")
        self._examples = list(spec.get("examples", []))

    @property
    def system_template(self) -> str:
        return self._system

    @property
    def user_template(self) -> str:
        return self._user

    @property
    def examples(self) -> list[dict]:
        return self._examples
