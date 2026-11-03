"""幂等去重存储：同键执行状态与结果缓存（带 TTL）."""
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class IdempotencyStatus(str, Enum):
    IN_PROGRESS = "in_progress"  # 首次调用执行中，同键并发请求应被拒绝
    SUCCEEDED = "succeeded"      # 执行成功，同键请求直接重放缓存结果
    FAILED = "failed"            # 执行失败，允许同键重试重新执行


@dataclass
class IdempotencyRecord:
    key: str
    tool_name: str
    tenant_id: str
    status: IdempotencyStatus
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    result: Any = None  # 成功时缓存的 ToolResult，供同键重放
    was_new: bool = False  # True 表示本次 begin 刚完成占位（放行执行）

    @property
    def composite_key(self) -> str:
        """幂等键在 (tenant_id, tool_name) 命名空间内唯一."""
        return f"{self.tenant_id}|{self.tool_name}|{self.key}"

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) > self.expires_at > 0


class IdempotencyStore(ABC):
    """去重存储抽象（生产环境可替换为 Redis SETNX / DB 唯一键实现）."""

    @abstractmethod
    def begin(self, tool_name: str, tenant_id: str, key: str, ttl_seconds: float) -> IdempotencyRecord:
        """原子占位：键不存在/已过期/上次失败 → 写入 IN_PROGRESS 并返回新记录（was_new=True）；
        否则返回既有记录（was_new=False），由守卫判定重放或冲突."""

    @abstractmethod
    def complete(self, record: IdempotencyRecord, result: Any) -> None:
        """执行成功：固化结果供同键重放."""

    @abstractmethod
    def fail(self, record: IdempotencyRecord) -> None:
        """执行失败：标记 FAILED，允许同键重试重新执行."""

    @abstractmethod
    def find(self, tool_name: str, tenant_id: str, key: str) -> Optional[IdempotencyRecord]:
        """查询记录（含惰性过期清理）."""


class InMemoryIdempotencyStore(IdempotencyStore):
    """进程内幂等存储：惰性 TTL 过期 + 线程安全占位."""

    def __init__(self):
        self._records: Dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _composite(tenant_id: str, tool_name: str, key: str) -> str:
        return f"{tenant_id}|{tool_name}|{key}"

    def begin(self, tool_name: str, tenant_id: str, key: str, ttl_seconds: float) -> IdempotencyRecord:
        composite = self._composite(tenant_id, tool_name, key)
        with self._lock:
            existing = self._records.get(composite)
            if existing is not None and not existing.is_expired():
                if existing.status in (IdempotencyStatus.SUCCEEDED, IdempotencyStatus.IN_PROGRESS):
                    existing.was_new = False
                    return existing
                # FAILED：允许重试，重新占位
            record = IdempotencyRecord(
                key=key,
                tool_name=tool_name,
                tenant_id=tenant_id,
                status=IdempotencyStatus.IN_PROGRESS,
                expires_at=time.time() + ttl_seconds,
                was_new=True,
            )
            self._records[composite] = record
            return record

    def complete(self, record: IdempotencyRecord, result: Any) -> None:
        with self._lock:
            if record.status == IdempotencyStatus.IN_PROGRESS:
                record.status = IdempotencyStatus.SUCCEEDED
                record.result = result

    def fail(self, record: IdempotencyRecord) -> None:
        with self._lock:
            if record.status == IdempotencyStatus.IN_PROGRESS:
                record.status = IdempotencyStatus.FAILED
                record.result = None

    def find(self, tool_name: str, tenant_id: str, key: str) -> Optional[IdempotencyRecord]:
        composite = self._composite(tenant_id, tool_name, key)
        with self._lock:
            record = self._records.get(composite)
            if record is not None and record.is_expired():
                del self._records[composite]
                return None
            return record
