"""PII (个人隐私与敏感信息) 脱敏掩码器."""
from __future__ import annotations

import re

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


class PIIMaskingGuardrail(BaseGuardrail):
    name: str = "pii_masking_guardrail"
    direction: GuardrailDirection = GuardrailDirection.BOTH
    default_severity: GuardrailSeverity = GuardrailSeverity.LOW
    default_action: GuardrailAction = GuardrailAction.MODIFY

    PHONE_REGEX = r"1[3-9]\d{9}"
    ID_CARD_REGEX = r"\d{17}[\dXx]"

    def __init__(self):
        super().__init__()
        self.register_rule("id_card_regex", {"name": "id_card_masking", "pattern": self.ID_CARD_REGEX})
        self.register_rule("phone_regex", {"name": "phone_masking", "pattern": self.PHONE_REGEX})

    def _check_impl(self, content: str, context: GuardrailContext) -> GuardrailResult:
        result = GuardrailResult()
        masked = content
        matched_any = False

        if context.direction != GuardrailDirection.OUTPUT and re.search(self.ID_CARD_REGEX, masked):
            masked = re.sub(self.ID_CARD_REGEX, "[ID_MASKED]", masked)
            matched_any = True
            result.add_violation(
                GuardrailViolation(
                    rule_id="pii_id_card",
                    rule_name="id_card_masking",
                    severity=GuardrailSeverity.MEDIUM,
                    action=GuardrailAction.MODIFY,
                    description="检测到身份证号并已脱敏遮罩",
                    confidence=1.0,
                )
            )

        if re.search(self.PHONE_REGEX, masked):
            masked = re.sub(self.PHONE_REGEX, "[PHONE_MASKED]", masked)
            matched_any = True
            result.add_violation(
                GuardrailViolation(
                    rule_id="pii_phone",
                    rule_name="phone_masking",
                    severity=GuardrailSeverity.LOW,
                    action=GuardrailAction.MODIFY,
                    description="检测到手机号并已脱敏遮罩",
                    confidence=1.0,
                )
            )

        if matched_any:
            result.modified_content = masked
            result.action = GuardrailAction.MODIFY

        return result

    async def validate_input(self, text: str) -> GuardrailCheckResult:
        masked = re.sub(self.ID_CARD_REGEX, "[ID_MASKED]", text)
        masked = re.sub(self.PHONE_REGEX, "[PHONE_MASKED]", masked)
        return GuardrailCheckResult(is_safe=True, sanitized_text=masked)

    async def validate_output(self, text: str) -> GuardrailCheckResult:
        masked = re.sub(self.PHONE_REGEX, "[PHONE_MASKED]", text)
        return GuardrailCheckResult(is_safe=True, sanitized_text=masked)
