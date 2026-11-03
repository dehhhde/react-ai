"""测试公共夹具：隔离进程级单例状态，防止用例间幂等/审批/弹性组件串扰."""
import pytest

from react_ai.approval.service import reset_approval_service
from react_ai.idempotency.guard import reset_idempotency_guard
from react_ai.tools.resilience import (
    reset_bulkhead,
    reset_circuit_breaker_registry,
    reset_rate_limiter,
    reset_result_cache,
)


@pytest.fixture(autouse=True)
def _reset_global_singletons():
    reset_idempotency_guard()
    reset_approval_service()
    reset_circuit_breaker_registry()
    reset_rate_limiter()
    reset_bulkhead()
    reset_result_cache()
    yield
    reset_idempotency_guard()
    reset_approval_service()
    reset_circuit_breaker_registry()
    reset_rate_limiter()
    reset_bulkhead()
    reset_result_cache()
