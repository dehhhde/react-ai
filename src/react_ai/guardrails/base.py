"""安全护栏拦截基类、上下文、结果契约与组合链机制."""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class GuardrailDirection(Enum):
    """护栏检查方向枚举，区分输入侧和输出侧。"""
    INPUT = "input"      # 输入护栏：检查用户请求、提示词等
    OUTPUT = "output"    # 输出护栏：检查模型响应、生成内容等
    BOTH = "both"        # 双向护栏：同时检查输入和输出


class GuardrailSeverity(Enum):
    """护栏违规严重程度枚举。"""
    LOW = 0       # 低：轻微违规，可记录但不阻断
    MEDIUM = 1    # 中：中度违规，建议警告或修正
    HIGH = 2      # 高：严重违规，应阻断并记录
    CRITICAL = 3  # 严重：极度危险，立即阻断并告警

    @classmethod
    def _missing_(cls, value: object):
        if isinstance(value, str):
            val_upper = value.upper()
            for member in cls:
                if member.name == val_upper:
                    return member
        return None


class GuardrailAction(Enum):
    """护栏触发动作枚举。"""
    ALLOW = "allow"          # 允许通过，不做任何处理
    WARN = "warn"            # 警告，记录日志但允许通过
    MODIFY = "modify"        # 修改内容后放行
    BLOCK = "block"          # 阻断，拒绝请求/响应
    REDIRECT = "redirect"    # 重定向到安全回复或兜底内容

    @classmethod
    def _missing_(cls, value: object):
        if isinstance(value, str):
            val_lower = value.lower()
            for member in cls:
                if member.value == val_lower or member.name == value.upper():
                    return member
        return None


class GuardrailCheckResult(BaseModel):
    """旧版兼容模型：轻量级检查结果."""
    is_safe: bool = True
    flagged_reason: str | None = None
    sanitized_text: str | None = None


@dataclass
class GuardrailViolation:
    """单次护栏违规记录。"""
    rule_id: str = ""                          # 触发的规则ID
    rule_name: str = ""                        # 规则名称
    severity: GuardrailSeverity = GuardrailSeverity.LOW  # 违规严重程度
    action: GuardrailAction = GuardrailAction.ALLOW       # 采取的动作
    description: str = ""                      # 违规描述
    matched_content: str = ""                  # 匹配到的具体内容片段
    confidence: float = 0.0                    # 判定置信度（0.0 ~ 1.0）
    timestamp: float = field(default_factory=time.time)  # 违规发生时间戳

    def to_dict(self) -> dict[str, Any]:
        """将违规记录转为字典，便于序列化。"""
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "action": self.action.value,
            "description": self.description,
            "matched_content": self.matched_content,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuardrailViolation:
        """从字典还原违规记录。"""
        severity_val = data.get("severity", GuardrailSeverity.LOW)
        severity = severity_val if isinstance(severity_val, GuardrailSeverity) else GuardrailSeverity(severity_val)
        action_val = data.get("action", GuardrailAction.ALLOW)
        action = action_val if isinstance(action_val, GuardrailAction) else GuardrailAction(action_val)
        return cls(
            rule_id=data.get("rule_id", ""),
            rule_name=data.get("rule_name", ""),
            severity=severity,
            action=action,
            description=data.get("description", ""),
            matched_content=data.get("matched_content", ""),
            confidence=float(data.get("confidence", 0.0)),
            timestamp=float(data.get("timestamp", time.time())),
        )


@dataclass
class GuardrailResult:
    """护栏检查的完整结果。"""
    check_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 本次检查的唯一ID
    passed: bool = True                     # 是否通过所有护栏检查
    action: GuardrailAction = GuardrailAction.ALLOW  # 最终采取的动作（取最严格的）
    violations: list[GuardrailViolation] = field(default_factory=list)  # 所有违规记录
    modified_content: str | None = None     # 修改后的内容（当 action 为 MODIFY 时有效）
    fallback_content: str | None = None     # 兜底内容（当 action 为 BLOCK/REDIRECT 时有效）
    latency_ms: float = 0.0                 # 检查耗时（毫秒）
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据

    def add_violation(self, violation: GuardrailViolation):
        """添加一条违规记录，并自动更新整体状态。
        
        Args:
            violation: 违规记录对象
        """
        self.violations.append(violation)  # 加入违规列表
        self.passed = False  # 有任何违规即视为未通过

        # 更新最终动作为最严格的那个
        action_priority = {
            GuardrailAction.ALLOW: 0,
            GuardrailAction.WARN: 1,
            GuardrailAction.MODIFY: 2,
            GuardrailAction.REDIRECT: 3,
            GuardrailAction.BLOCK: 4,
        }
        current_priority = action_priority.get(self.action, 0)  # 当前动作优先级
        new_priority = action_priority.get(violation.action, 0)  # 新违规动作优先级
        if new_priority > current_priority:
            self.action = violation.action  # 取更严格的动作

    def to_dict(self) -> dict[str, Any]:
        """将检查结果转为字典，便于日志和审计。"""
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "action": self.action.value,
            "violations": [v.to_dict() for v in self.violations],
            "modified_content": self.modified_content,
            "fallback_content": self.fallback_content,
            "latency_ms": round(self.latency_ms, 2),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuardrailResult:
        """从字典还原检查结果。"""
        action_val = data.get("action", GuardrailAction.ALLOW)
        action = action_val if isinstance(action_val, GuardrailAction) else GuardrailAction(action_val)
        violations = [
            GuardrailViolation.from_dict(v) if isinstance(v, dict) else v
            for v in data.get("violations", [])
        ]
        return cls(
            check_id=data.get("check_id", uuid.uuid4().hex),
            passed=bool(data.get("passed", True)),
            action=action,
            violations=violations,
            modified_content=data.get("modified_content"),
            fallback_content=data.get("fallback_content"),
            latency_ms=float(data.get("latency_ms", 0.0)),
            metadata=dict(data.get("metadata", {})),
        )

    def to_check_result(self) -> GuardrailCheckResult:
        """转换为旧版兼容模型 GuardrailCheckResult."""
        flagged = None
        if self.violations:
            flagged = "; ".join(
                f"[{v.rule_name or v.rule_id}] {v.description}" if (v.rule_name or v.rule_id) else v.description
                for v in self.violations
            )
        return GuardrailCheckResult(
            is_safe=self.passed and self.action != GuardrailAction.BLOCK,
            flagged_reason=flagged,
            sanitized_text=self.modified_content,
        )


@dataclass
class GuardrailContext:
    """护栏检查的上下文信息。"""
    request_id: str = ""                     # 请求ID，用于链路追踪
    session_id: str = ""                     # 会话ID
    user_id: str | None = None               # 用户ID
    tenant_id: str | None = None             # 租户ID
    direction: GuardrailDirection = GuardrailDirection.INPUT  # 检查方向
    content_type: str = "text/plain"         # 内容类型
    model_name: str = ""                     # 关联的模型名称（输出护栏时有效）
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展上下文


class BaseGuardrail(ABC):
    """护栏基类：为 LLM / Agent 系统提供统一的输入/输出安全校验抽象。

    设计目标：
    - 统一护栏接口，支持规则引擎、关键词、正则、模型分类等多种检测方式；
    - 支持输入护栏（请求侧）和输出护栏（响应侧）；
    - 提供可组合的护栏链，多个护栏可串行/并行执行；
    - 内置审计日志、指标统计和熔断保护；
    - 可扩展，子类只需实现核心检测方法。
    """

    name: str = "base_guardrail"  # 护栏名称，用于日志和标识
    version: str = "1.0"  # 护栏版本
    enabled: bool = True  # 是否启用该护栏
    direction: GuardrailDirection = GuardrailDirection.BOTH  # 默认检查方向
    default_severity: GuardrailSeverity = GuardrailSeverity.MEDIUM  # 默认违规严重程度
    default_action: GuardrailAction = GuardrailAction.BLOCK  # 默认触发动作
    timeout_seconds: float = 5.0  # 单次检查超时时间（秒）
    max_failures: int = 10  # 连续失败次数阈值，超过则熔断

    def __init__(self):
        self._lock = threading.RLock()  # 线程锁，保护共享状态
        self._check_count: int = 0  # 累计检查次数
        self._pass_count: int = 0  # 累计通过次数
        self._block_count: int = 0  # 累计阻断次数
        self._error_count: int = 0  # 累计错误次数
        self._consecutive_failures: int = 0  # 连续失败次数（用于熔断）
        self._circuit_open: bool = False  # 熔断器状态
        self._circuit_opened_at: float = 0.0  # 熔断开启时间
        self._circuit_cooldown: float = 60.0  # 熔断冷却时间（秒）
        self._rules: dict[str, dict[str, Any]] = {}  # 规则注册表
        self._pre_hooks: list[Callable] = []  # 检查前钩子
        self._post_hooks: list[Callable] = []  # 检查后钩子

    def register_rule(self, rule_id: str, rule_config: dict[str, Any]):
        """注册一条检测规则。
        
        Args:
            rule_id: 规则唯一ID
            rule_config: 规则配置字典，包含 name, severity, action, pattern 等
        """
        with self._lock:
            self._rules[rule_id] = rule_config
            logger.info("guardrail_rule_registered name=%s rule_id=%s", self.name, rule_id)

    def unregister_rule(self, rule_id: str) -> bool:
        """注销一条检测规则。
        
        Args:
            rule_id: 规则ID
            
        Returns:
            是否成功注销
        """
        with self._lock:
            if rule_id in self._rules:
                del self._rules[rule_id]
                logger.info("guardrail_rule_unregistered name=%s rule_id=%s", self.name, rule_id)
                return True
            return False

    def add_pre_hook(self, hook: Callable):
        """添加检查前钩子函数。
        
        Args:
            hook: 回调函数，接收 (content, context) 参数
        """
        with self._lock:
            self._pre_hooks.append(hook)

    def add_post_hook(self, hook: Callable):
        """添加检查后钩子函数。
        
        Args:
            hook: 回调函数，接收 (content, result, context) 参数
        """
        with self._lock:
            self._post_hooks.append(hook)

    def check(self, content: str, context: GuardrailContext | None = None) -> GuardrailResult:
        """执行护栏检查的主入口。
        
        Args:
            content: 待检查的内容（用户输入或模型输出）
            context: 检查上下文，包含请求ID、方向等信息
            
        Returns:
            护栏检查结果
        """
        start_time = time.time()  # 记录开始时间
        context = context or GuardrailContext()  # 使用传入上下文或新建

        # 检查是否启用
        if not self.enabled:
            result = GuardrailResult(passed=True, action=GuardrailAction.ALLOW)
            result.latency_ms = (time.time() - start_time) * 1000  # 计算耗时
            return result

        # 检查熔断状态
        if self._is_circuit_open():
            result = GuardrailResult(
                passed=True,
                action=GuardrailAction.ALLOW,
                metadata={"circuit_open": True, "reason": "guardrail_circuit_breaker_open"},
            )
            result.latency_ms = (time.time() - start_time) * 1000
            logger.warning("guardrail_circuit_open name=%s", self.name)
            return result

        # 检查方向是否匹配
        if not self._should_check(context.direction):
            result = GuardrailResult(passed=True, action=GuardrailAction.ALLOW)
            result.latency_ms = (time.time() - start_time) * 1000
            return result

        # 执行检查前钩子
        for hook in self._pre_hooks:
            try:
                hook(content, context)
            except Exception as exc:  # noqa: BLE001
                logger.error("guardrail_pre_hook_error name=%s error=%s", self.name, str(exc))

        # 执行核心检测
        result = self._do_check(content, context)

        # 记录耗时
        result.latency_ms = (time.time() - start_time) * 1000

        # 更新统计
        with self._lock:
            self._check_count += 1
            if result.passed:
                self._pass_count += 1
                self._consecutive_failures = 0  # 检查成功，重置连续失败计数
            else:
                if result.action == GuardrailAction.BLOCK:
                    self._block_count += 1
            if not result.passed and result.action == GuardrailAction.BLOCK:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.max_failures:
                    self._circuit_open = True
                    self._circuit_opened_at = time.time()
                    logger.error(
                        "guardrail_circuit_breaker_triggered name=%s failures=%d",
                        self.name, self._consecutive_failures,
                    )

        # 执行检查后钩子
        for hook in self._post_hooks:
            try:
                hook(content, result, context)
            except Exception as exc:  # noqa: BLE001
                logger.error("guardrail_post_hook_error name=%s error=%s", self.name, str(exc))

        # 记录审计日志
        self._log_audit(content, result, context)

        return result

    def _do_check(self, content: str, context: GuardrailContext) -> GuardrailResult:
        """执行核心检测逻辑，带异常保护。
        
        Args:
            content: 待检查内容
            context: 检查上下文
            
        Returns:
            护栏检查结果
        """
        result = GuardrailResult()
        try:
            # 委托给子类实现的具体检测方法
            result = self._check_impl(content, context)
        except Exception as exc:
            with self._lock:
                self._error_count += 1
                self._consecutive_failures += 1
            logger.exception("guardrail_check_error name=%s", self.name)
            # 出错时默认放行，避免影响主流程
            result.passed = True
            result.action = GuardrailAction.ALLOW
            result.metadata["error"] = str(exc)
        return result

    @abstractmethod
    def _check_impl(self, content: str, context: GuardrailContext) -> GuardrailResult:
        """子类必须实现的核心检测方法。
        
        Args:
            content: 待检查内容
            context: 检查上下文
            
        Returns:
            护栏检查结果
        """

    def _should_check(self, direction: GuardrailDirection) -> bool:
        """判断当前方向是否需要检查。
        
        Args:
            direction: 当前检查方向
            
        Returns:
            是否需要检查
        """
        if self.direction == GuardrailDirection.BOTH:
            return True  # 双向都检查
        if self.direction == GuardrailDirection.INPUT:
            return direction == GuardrailDirection.INPUT  # 仅检查输入
        if self.direction == GuardrailDirection.OUTPUT:
            return direction == GuardrailDirection.OUTPUT  # 仅检查输出
        return False

    def _is_circuit_open(self) -> bool:
        """检查熔断器是否处于开启状态。
        
        Returns:
            是否熔断
        """
        if not self._circuit_open:
            return False
        # 检查是否过了冷却期
        elapsed = time.time() - self._circuit_opened_at
        if elapsed >= self._circuit_cooldown:
            self._circuit_open = False  # 冷却期结束，恢复
            self._consecutive_failures = 0
            logger.info("guardrail_circuit_recovered name=%s", self.name)
            return False
        return True

    def _log_audit(self, content: str, result: GuardrailResult, context: GuardrailContext):
        """记录审计日志。
        
        Args:
            content: 检查内容
            result: 检查结果
            context: 检查上下文
        """
        if not result.passed:
            logger.warning(
                "guardrail_violation name=%s check_id=%s action=%s violations=%d latency=%.1fms request_id=%s",
                self.name, result.check_id, result.action.value,
                len(result.violations), result.latency_ms, context.request_id,
            )
            for v in result.violations:
                logger.debug(
                    "guardrail_violation_detail rule_id=%s severity=%s confidence=%.2f matched=%s",
                    v.rule_id, v.severity.value, v.confidence,
                    v.matched_content[:100] if v.matched_content else "",
                )

    def get_stats(self) -> dict[str, Any]:
        """获取护栏运行统计信息。
        
        Returns:
            统计字典
        """
        with self._lock:
            pass_rate = (self._pass_count / self._check_count * 100) if self._check_count > 0 else 0.0
            return {
                "name": self.name,
                "version": self.version,
                "enabled": self.enabled,
                "direction": self.direction.value,
                "check_count": self._check_count,
                "pass_count": self._pass_count,
                "block_count": self._block_count,
                "error_count": self._error_count,
                "pass_rate": round(pass_rate, 2),
                "consecutive_failures": self._consecutive_failures,
                "circuit_open": self._circuit_open,
                "rules_count": len(self._rules),
            }

    def reset_stats(self):
        """重置统计信息。"""
        with self._lock:
            self._check_count = 0
            self._pass_count = 0
            self._block_count = 0
            self._error_count = 0
            self._consecutive_failures = 0
            self._circuit_open = False
            self._circuit_opened_at = 0.0
            logger.info("guardrail_stats_reset name=%s", self.name)

    async def validate_input(self, text: str) -> GuardrailCheckResult:
        """异步输入校验接口（向后兼容旧版协程协议）."""
        context = GuardrailContext(direction=GuardrailDirection.INPUT)
        res = self.check(text, context)
        flagged = None
        if not res.passed and res.violations:
            flagged = res.violations[0].description
        return GuardrailCheckResult(
            is_safe=res.passed and res.action != GuardrailAction.BLOCK,
            flagged_reason=flagged,
            sanitized_text=res.modified_content,
        )

    async def validate_output(self, text: str) -> GuardrailCheckResult:
        """异步输出校验接口（向后兼容旧版协程协议）."""
        context = GuardrailContext(direction=GuardrailDirection.OUTPUT)
        res = self.check(text, context)
        flagged = None
        if not res.passed and res.violations:
            flagged = res.violations[0].description
        return GuardrailCheckResult(
            is_safe=res.passed and res.action != GuardrailAction.BLOCK,
            flagged_reason=flagged,
            sanitized_text=res.modified_content,
        )


class KeywordGuardrail(BaseGuardrail):
    """关键词护栏：基于关键词/正则表达式进行内容检测。

    适用于敏感词过滤、PII 检测、指令注入防护等场景。
    """

    name: str = "keyword_guardrail"

    def __init__(self):
        super().__init__()
        self._compiled_patterns: dict[str, re.Pattern] = {}  # 预编译的正则缓存

    def register_rule(self, rule_id: str, rule_config: dict[str, Any]):
        """注册关键词/正则规则。
        
        Args:
            rule_id: 规则ID
            rule_config: 包含 pattern, severity, action, case_sensitive 等
        """
        pattern_str = rule_config.get("pattern", "")
        if not pattern_str:
            logger.warning("guardrail_empty_pattern rule_id=%s", rule_id)
            return
        try:
            flags = 0 if rule_config.get("case_sensitive", False) else re.IGNORECASE
            compiled = re.compile(pattern_str, flags)
            self._compiled_patterns[rule_id] = compiled
        except re.error as exc:
            logger.error("guardrail_invalid_regex rule_id=%s pattern=%s error=%s", rule_id, pattern_str, str(exc))
            return
        super().register_rule(rule_id, rule_config)

    def unregister_rule(self, rule_id: str) -> bool:
        """注销规则，同时清除编译缓存。
        
        Args:
            rule_id: 规则ID
            
        Returns:
            是否成功注销
        """
        with self._lock:
            self._compiled_patterns.pop(rule_id, None)
        return super().unregister_rule(rule_id)

    def _check_impl(self, content: str, context: GuardrailContext) -> GuardrailResult:
        """基于关键词/正则进行匹配检测。
        
        Args:
            content: 待检查内容
            context: 检查上下文
            
        Returns:
            护栏检查结果
        """
        result = GuardrailResult()

        with self._lock:
            rules_snapshot = dict(self._rules)
            patterns_snapshot = dict(self._compiled_patterns)

        for rule_id, rule_config in rules_snapshot.items():
            pattern = patterns_snapshot.get(rule_id)
            if not pattern:
                continue

            match = pattern.search(content)
            if not match:
                continue

            # 命中规则，创建违规记录
            severity_val = rule_config.get("severity", self.default_severity)
            action_val = rule_config.get("action", self.default_action)

            try:
                severity = severity_val if isinstance(severity_val, GuardrailSeverity) else GuardrailSeverity(severity_val)
            except (ValueError, KeyError):
                severity = self.default_severity

            try:
                action = action_val if isinstance(action_val, GuardrailAction) else GuardrailAction(action_val)
            except (ValueError, KeyError):
                action = self.default_action

            violation = GuardrailViolation(
                rule_id=rule_id,
                rule_name=rule_config.get("name", rule_id),
                severity=severity,
                action=action,
                description=rule_config.get("description", f"命中规则: {rule_id}"),
                matched_content=match.group(0)[:200],
                confidence=1.0,
            )
            result.add_violation(violation)

            # 如果动作是 BLOCK，可以提前返回
            if action == GuardrailAction.BLOCK:
                break

        return result


class GuardrailChain:
    """护栏链：将多个护栏组合成一条检查链，串行执行。

    支持短路策略：一旦某个护栏阻断，后续护栏不再执行。
    """

    def __init__(self, name: str = "guardrail_chain", short_circuit: bool = True):
        self.name = name
        self.short_circuit = short_circuit  # 是否短路
        self._guardrails: list[BaseGuardrail] = []
        self._lock = threading.RLock()

    def add_guardrail(self, guardrail: BaseGuardrail, position: int | None = None):
        """添加护栏到链中。
        
        Args:
            guardrail: 护栏实例
            position: 插入位置，None 则追加到末尾
        """
        with self._lock:
            if position is not None:
                self._guardrails.insert(position, guardrail)
            else:
                self._guardrails.append(guardrail)
            logger.info("guardrail_chain_add chain=%s guardrail=%s", self.name, guardrail.name)

    def remove_guardrail(self, guardrail: BaseGuardrail) -> bool:
        """从链中移除护栏。
        
        Args:
            guardrail: 要移除的护栏实例
            
        Returns:
            是否成功移除
        """
        with self._lock:
            if guardrail in self._guardrails:
                self._guardrails.remove(guardrail)
                logger.info("guardrail_chain_remove chain=%s guardrail=%s", self.name, guardrail.name)
                return True
            return False

    def check(self, content: str, context: GuardrailContext | None = None) -> GuardrailResult:
        """串行执行护栏链中的所有护栏。
        
        Args:
            content: 待检查内容
            context: 检查上下文
            
        Returns:
            综合护栏检查结果
        """
        start_time = time.time()
        context = context or GuardrailContext()
        aggregate_result = GuardrailResult(passed=True, action=GuardrailAction.ALLOW)

        current_content = content
        with self._lock:
            guardrails_to_run = list(self._guardrails)

        for guardrail in guardrails_to_run:
            res = guardrail.check(current_content, context)
            if not res.passed:
                aggregate_result.passed = False
                for v in res.violations:
                    aggregate_result.add_violation(v)
                if res.fallback_content:
                    aggregate_result.fallback_content = res.fallback_content
                if self.short_circuit and res.action == GuardrailAction.BLOCK:
                    break

            if res.modified_content is not None:
                current_content = res.modified_content
                aggregate_result.modified_content = current_content

        aggregate_result.latency_ms = (time.time() - start_time) * 1000
        return aggregate_result

    def get_guardrails(self) -> list[BaseGuardrail]:
        """获取当前链中的所有护栏。"""
        with self._lock:
            return list(self._guardrails)

    def clear(self):
        """清空链中的护栏。"""
        with self._lock:
            self._guardrails.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._guardrails)
