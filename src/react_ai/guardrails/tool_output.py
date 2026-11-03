"""工具输出护栏：在 Observation 进入下一轮 prompt 前做 PII 脱敏与间接提示注入扫描.

工具输出（尤其是 HTTP/检索等外部数据源）是间接提示注入的主要载体，
且可能包含敏感信息（如手机号、身份证号）。本模块在 Observation 序列化后、
写入 messages 之前对其进行统一清洗。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from react_ai.guardrails.base import (
    GuardrailAction,
    GuardrailChain,
    GuardrailContext,
    GuardrailDirection,
)
from react_ai.guardrails.injection import PromptInjectionGuardrail
from react_ai.guardrails.pii import PIIMaskingGuardrail

logger = logging.getLogger(__name__)


def build_tool_output_chain() -> GuardrailChain:
    """构建工具输出专用护栏链：PII 脱敏（MODIFY）+ 提示注入拦截（BLOCK）."""
    chain = GuardrailChain(name="tool_output_chain", short_circuit=False)
    chain.add_guardrail(PIIMaskingGuardrail())
    chain.add_guardrail(PromptInjectionGuardrail())
    return chain


# 单例：进程内复用同一条链
_default_chain: GuardrailChain | None = None


def get_tool_output_chain() -> GuardrailChain:
    global _default_chain
    if _default_chain is None:
        _default_chain = build_tool_output_chain()
    return _default_chain


def serialize_observation(data: Any) -> str:
    """将工具输出序列化为 JSON 字符串（替代 str() 的 Python repr）."""
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(data)


def sanitize_observation(
    content: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
    chain: GuardrailChain | None = None,
) -> str:
    """对 Observation 做 PII 脱敏 + 提示注入扫描，返回最终写入 messages 的文本.

    - PII 命中：脱敏后放行（MODIFY）。
    - 注入命中（BLOCK）：用安全兜底文本替代，禁止原始内容进入 prompt。
    """
    chain = chain or get_tool_output_chain()
    # 工具输出被视为模型下一轮的"潜在输入"，统一用 INPUT 方向：
    # - PII 护栏（BOTH 方向）会脱敏手机号与身份证号；
    # - 注入护栏（INPUT 方向）会扫描间接提示注入特征。
    scan_ctx = GuardrailContext(
        direction=GuardrailDirection.INPUT,
        tenant_id=tenant_id or "",
        user_id=user_id or "",
        request_id=trace_id or "",
    )
    res = chain.check(content, scan_ctx)

    current = res.modified_content if res.modified_content is not None else content

    if not res.passed and res.action == GuardrailAction.BLOCK:
        reason = "; ".join(v.description for v in res.violations if v.action == GuardrailAction.BLOCK)
        reason = reason or "检测到潜在提示注入"
        logger.warning("tool_output_injection_blocked trace=%s reason=%s", trace_id, reason)
        return (
            f"[工具输出已被安全策略拦截]\n"
            f"原因：检测到潜在提示注入或越狱特征，原始内容未注入模型上下文。\n"
            f"说明：{reason}"
        )

    return current
