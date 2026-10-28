"""Prompt 注入与越狱拦截器."""
from __future__ import annotations

import re
from typing import ClassVar

from react_ai.guardrails.base import (
    BaseGuardrail,
    GuardrailAction,
    GuardrailCheckResult,
    GuardrailContext,
    GuardrailDirection,
    GuardrailResult,
    GuardrailSeverity,
    GuardrailViolation,
)


class PromptInjectionGuardrail(BaseGuardrail):
    name: str = "prompt_injection_guardrail"
    direction: GuardrailDirection = GuardrailDirection.INPUT
    default_severity: GuardrailSeverity = GuardrailSeverity.HIGH
    default_action: GuardrailAction = GuardrailAction.BLOCK

    INJECTION_PATTERNS: ClassVar[list[str]] = [
        r"ignore\s+(all\s+)?(previous|above)\s+instructions",
        r"system\s*prompt\s*override",
        r"you\s+are\s+now\s+in\s+developer\s+mode",
        r"忽略(之前|以上)(所有)?(指示|命令)",
    ]

    def __init__(self):
        super().__init__()
        for i, pattern in enumerate(self.INJECTION_PATTERNS):
            self.register_rule(
                rule_id=f"injection_pattern_{i}",
                rule_config={
                    "name": f"injection_rule_{i}",
                    "pattern": pattern,
                    "severity": GuardrailSeverity.HIGH,
                    "action": GuardrailAction.BLOCK,
                    "description": f"检测到潜在 Prompt 注入或越狱特征: {pattern}",
                },
            )

    def _check_impl(self, content: str, context: GuardrailContext) -> GuardrailResult:
        result = GuardrailResult()
        for i, pattern in enumerate(self.INJECTION_PATTERNS):
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                violation = GuardrailViolation(
                    rule_id=f"injection_pattern_{i}",
                    rule_name=f"injection_rule_{i}",
                    severity=GuardrailSeverity.HIGH,
                    action=GuardrailAction.BLOCK,
                    description=f"检测到潜在 Prompt 注入或越狱特征: {pattern}",
                    matched_content=match.group(0),
                    confidence=1.0,
                )
                result.add_violation(violation)
                break
        return result

    async def validate_input(self, text: str) -> GuardrailCheckResult:
        for pattern in self.INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return GuardrailCheckResult(
                    is_safe=False,
                    flagged_reason=f"检测到潜在 Prompt 注入或越狱特征: {pattern}",
                )
        return GuardrailCheckResult(is_safe=True)

    async def validate_output(self, text: str) -> GuardrailCheckResult:
        return GuardrailCheckResult(is_safe=True)
