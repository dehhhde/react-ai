"""幂等守卫：写操作执行前的同键去重判定."""
from dataclasses import dataclass
from typing import Any, Optional

from react_ai.config.settings import settings
from react_ai.idempotency.store import (
    IdempotencyRecord,
    IdempotencyStatus,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)


@dataclass
class IdempotencyDecision:
    action: str  # proceed（放行执行）| replay（重放缓存结果）| conflict（同键执行中）
    result: Any = None
    record: Optional[IdempotencyRecord] = None


class IdempotencyGuard:
    def __init__(self, store: Optional[IdempotencyStore] = None, ttl_seconds: Optional[float] = None):
        self.store = store or InMemoryIdempotencyStore()
        self.ttl_seconds = (
            ttl_seconds
            if ttl_seconds is not None
            else settings.security.idempotency_ttl_seconds
        )

    def begin(self, tool_name: str, tenant_id: str, key: str) -> IdempotencyDecision:
        """占位判定。仅由写/破坏性工具在幂等键校验通过后调用."""
        if not settings.security.enable_idempotency:
            return IdempotencyDecision(action="proceed")
        record = self.store.begin(tool_name, tenant_id, key, self.ttl_seconds)
        if record.was_new:
            return IdempotencyDecision(action="proceed", record=record)
        if record.status == IdempotencyStatus.SUCCEEDED:
            return IdempotencyDecision(action="replay", result=record.result, record=record)
        if record.status == IdempotencyStatus.IN_PROGRESS:
            return IdempotencyDecision(action="conflict", record=record)
        return IdempotencyDecision(action="proceed", record=record)

    def success(self, decision: IdempotencyDecision, result: Any) -> None:
        if decision.record is not None:
            self.store.complete(decision.record, result)

    def failure(self, decision: IdempotencyDecision) -> None:
        if decision.record is not None:
            self.store.fail(decision.record)


_default_guard: Optional[IdempotencyGuard] = None


def get_idempotency_guard() -> IdempotencyGuard:
    """进程级单例，供 BaseTool 执行链路共享."""
    global _default_guard
    if _default_guard is None:
        _default_guard = IdempotencyGuard()
    return _default_guard


def reset_idempotency_guard(guard: Optional[IdempotencyGuard] = None) -> IdempotencyGuard:
    """注入新实例（测试/多租户场景），返回生效实例."""
    global _default_guard
    _default_guard = guard if guard is not None else IdempotencyGuard()
    return _default_guard
