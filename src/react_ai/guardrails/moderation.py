"""敏感词合规审核与风控拦截."""
from __future__ import annotations

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


class ModerationGuardrail(BaseGuardrail):
    name: str = "moderation_guardrail"
    direction: GuardrailDirection = GuardrailDirection.BOTH
    default_severity: GuardrailSeverity = GuardrailSeverity.HIGH
    default_action: GuardrailAction = GuardrailAction.BLOCK

    def __init__(self, blocklist: set[str] | None = None):
        super().__init__()
        self.blocklist = set(blocklist) if blocklist else set()
        for word in self.blocklist:
            self.register_rule(
                rule_id=f"word_{word}",
                rule_config={
                    "name": "sensitive_word",
                    "pattern": word,
                    "severity": GuardrailSeverity.HIGH,
                    "action": GuardrailAction.BLOCK,
                    "description": f"触发企业合规敏感词限制: {word}",
                },
            )

    def _check_impl(self, content: str, context: GuardrailContext) -> GuardrailResult:
        result = GuardrailResult()
        for word in self.blocklist:
            if word in content:
                desc = (
                    f"触发企业合规敏感词限制: {word}"
                    if context.direction == GuardrailDirection.INPUT
                    else "输出内容违反企业合规策略"
                )
                violation = GuardrailViolation(
                    rule_id=f"sensitive_{word}",
                    rule_name="moderation_blocklist",
                    severity=GuardrailSeverity.HIGH,
                    action=GuardrailAction.BLOCK,
                    description=desc,
                    matched_content=word,
                    confidence=1.0,
                )
                result.add_violation(violation)
                break
        return result

    async def validate_input(self, text: str) -> GuardrailCheckResult:
        for word in self.blocklist:
            if word in text:
                return GuardrailCheckResult(is_safe=False, flagged_reason=f"触发企业合规敏感词限制: {word}")
        return GuardrailCheckResult(is_safe=True)

    async def validate_output(self, text: str) -> GuardrailCheckResult:
        for word in self.blocklist:
            if word in text:
                return GuardrailCheckResult(is_safe=False, flagged_reason="输出内容违反企业合规策略")
        return GuardrailCheckResult(is_safe=True)
