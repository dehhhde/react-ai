"""工具调用韧性层：熔断器、令牌桶限流、舱壁信号量、结果缓存.

企业级工具调用必备的横切能力，全部以可选方式接入 ToolSandbox：
- CircuitBreaker：按工具维度统计失败率，超阈值快速失败，冷却后半开探测。
- RateLimiter：tenant/user/tool 三维令牌桶，超限返回可回喂的 rate_limited 错误。
- Bulkhead：每工具并发信号量 + 全局 worker 上限，防止慢工具拖垮整个事件循环。
- ResultCache：read/none 副作用工具的短 TTL 缓存，参数+租户为 key，省成本降延迟。

所有组件均设计为线程/协程安全，可在多并发请求下共享单例。
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from react_ai.tools.base import ToolResult


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """按工具维度的熔断器：closed -> open -> half-open 状态机.

    - closed：正常放行，统计失败率；
    - open：失败率超阈值或连续失败超阈值，快速失败；
    - half-open：冷却后放少量探测请求，成功则闭合，失败则重新打开。
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        failure_rate_threshold: float = 0.5,
        window_seconds: float = 60.0,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.failure_rate_threshold = failure_rate_threshold
        self.window_seconds = window_seconds
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state: str = "closed"  # closed | open | half-open
        self._opened_at: float = 0.0
        self._window: list[Tuple[float, bool]] = []  # (timestamp, success)
        self._half_open_calls: int = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow_request(self) -> bool:
        """是否允许本次请求通过。open 状态下过了冷却期进入 half-open 探测。"""
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.time() - self._opened_at >= self.recovery_timeout:
                    self._state = "half-open"
                    self._half_open_calls = 0
                    return True
                return False
            # half-open：仅允许有限个探测请求
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False

    def record(self, success: bool) -> None:
        with self._lock:
            now = time.time()
            if self._state == "half-open":
                if success:
                    self._state = "closed"
                    self._window.clear()
                else:
                    self._state = "open"
                    self._opened_at = now
                self._half_open_calls = 0
                return

            # closed 状态下滑动窗口统计
            self._window.append((now, success))
            cutoff = now - self.window_seconds
            self._window = [(t, ok) for t, ok in self._window if t >= cutoff]

            if not self._window:
                return
            failures = sum(1 for _, ok in self._window if not ok)
            failure_rate = failures / len(self._window)
            if failures >= self.failure_threshold and failure_rate >= self.failure_rate_threshold:
                self._state = "open"
                self._opened_at = now

    def reset(self) -> None:
        with self._lock:
            self._state = "closed"
            self._window.clear()
            self._half_open_calls = 0
            self._opened_at = 0.0


class CircuitBreakerRegistry:
    """按工具名维护熔断器实例，全局单例."""

    def __init__(self, **breaker_kwargs: Any):
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._kwargs = breaker_kwargs
        self._lock = threading.Lock()

    def get(self, tool_name: str) -> CircuitBreaker:
        with self._lock:
            cb = self._breakers.get(tool_name)
            if cb is None:
                cb = CircuitBreaker(**self._kwargs)
                self._breakers[tool_name] = cb
            return cb

    def all(self) -> Dict[str, CircuitBreaker]:
        with self._lock:
            return dict(self._breakers)


_default_breaker_registry: Optional[CircuitBreakerRegistry] = None


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    global _default_breaker_registry
    if _default_breaker_registry is None:
        _default_breaker_registry = CircuitBreakerRegistry()
    return _default_breaker_registry


def reset_circuit_breaker_registry() -> None:
    global _default_breaker_registry
    _default_breaker_registry = None


# ---------------------------------------------------------------------------
# 令牌桶限流
# ---------------------------------------------------------------------------

class TokenBucket:
    """令牌桶限流器：按固定速率补充令牌，请求消耗令牌，无令牌则拒绝."""

    def __init__(self, rate: float, capacity: float):
        self.rate = rate  # 每秒补充令牌数
        self.capacity = capacity  # 桶容量（最大突发）
        self._tokens: float = capacity
        self._last_refill: float = time.time()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


class RateLimiter:
    """tenant / user / tool 三维令牌桶限流."""

    def __init__(self, default_qps: float = 10.0, default_burst: float = 20.0):
        self.default_qps = default_qps
        self.default_burst = default_burst
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _key(self, tenant_id: str, user_id: str, tool_name: str) -> str:
        return f"{tenant_id}|{user_id}|{tool_name}"

    def _get_bucket(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self.default_qps, self.default_burst)
            self._buckets[key] = bucket
        return bucket

    def allow(self, tenant_id: str, user_id: str, tool_name: str) -> bool:
        key = self._key(tenant_id, user_id, tool_name)
        with self._lock:
            bucket = self._get_bucket(key)
        return bucket.try_acquire()


_default_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    global _default_rate_limiter
    if _default_rate_limiter is None:
        _default_rate_limiter = RateLimiter()
    return _default_rate_limiter


def reset_rate_limiter() -> None:
    global _default_rate_limiter
    _default_rate_limiter = None


# ---------------------------------------------------------------------------
# 舱壁（并发上限）
# ---------------------------------------------------------------------------

class Bulkhead:
    """每工具并发信号量 + 全局 worker 上限，实现资源隔离."""

    def __init__(self, per_tool_concurrency: int = 10, global_concurrency: int = 100):
        self.per_tool_concurrency = per_tool_concurrency
        self.global_semaphore = asyncio.Semaphore(global_concurrency)
        self._tool_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._lock = threading.Lock()

    def _get_tool_semaphore(self, tool_name: str) -> asyncio.Semaphore:
        sem = self._tool_semaphores.get(tool_name)
        if sem is None:
            sem = asyncio.Semaphore(self.per_tool_concurrency)
            self._tool_semaphores[tool_name] = sem
        return sem

    async def acquire(self, tool_name: str) -> Tuple[asyncio.Semaphore, asyncio.Semaphore]:
        with self._lock:
            tool_sem = self._get_tool_semaphore(tool_name)
        await tool_sem.acquire()
        try:
            await self.global_semaphore.acquire()
        except Exception:
            tool_sem.release()
            raise
        return tool_sem, self.global_semaphore

    @staticmethod
    def release(tool_sem: asyncio.Semaphore, global_sem: asyncio.Semaphore) -> None:
        tool_sem.release()
        global_sem.release()


_default_bulkhead: Optional[Bulkhead] = None


def get_bulkhead() -> Bulkhead:
    global _default_bulkhead
    if _default_bulkhead is None:
        _default_bulkhead = Bulkhead()
    return _default_bulkhead


def reset_bulkhead() -> None:
    global _default_bulkhead
    _default_bulkhead = None


# ---------------------------------------------------------------------------
# 结果缓存
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    result: ToolResult
    expires_at: float = 0.0


class ResultCache:
    """read/none 副作用工具的短 TTL 结果缓存.

    key = sha256(tool_name + tenant_id + sorted(arguments))，带 TTL 惰性过期。
    """

    def __init__(self, ttl_seconds: float = 60.0, max_entries: int = 1024):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._store: Dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _make_key(tool_name: str, tenant_id: str, arguments: Dict[str, Any]) -> str:
        payload = f"{tool_name}|{tenant_id}|{repr(sorted(arguments.items()))}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def get(self, tool_name: str, tenant_id: str, arguments: Dict[str, Any]) -> Optional[ToolResult]:
        key = self._make_key(tool_name, tenant_id, arguments)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at and time.time() > entry.expires_at:
                del self._store[key]
                return None
            return entry.result

    def set(self, tool_name: str, tenant_id: str, arguments: Dict[str, Any], result: ToolResult) -> None:
        if not result.ok:
            return  # 仅缓存成功结果
        key = self._make_key(tool_name, tenant_id, arguments)
        with self._lock:
            if len(self._store) >= self.max_entries:
                # 简单淘汰：删除最早的一条（dict 有序）
                oldest = next(iter(self._store))
                del self._store[oldest]
            self._store[key] = CacheEntry(result=result, expires_at=time.time() + self.ttl_seconds)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_default_result_cache: Optional[ResultCache] = None


def get_result_cache() -> ResultCache:
    global _default_result_cache
    if _default_result_cache is None:
        _default_result_cache = ResultCache()
    return _default_result_cache


def reset_result_cache() -> None:
    global _default_result_cache
    _default_result_cache = None
