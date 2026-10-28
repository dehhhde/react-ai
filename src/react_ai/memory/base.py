import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class MemoryType(Enum):
    """记忆类型枚举，用于区分不同用途的记忆。"""

    SESSION = "session"  # 会话记忆：当前对话窗口的短期上下文
    WORKING = "working"  # 工作记忆：当前任务执行中的中间状态
    LONG_TERM = "long_term"  # 长期记忆：跨会话的用户偏好、历史摘要
    TOOL_RESULT = "tool_result"  # 工具结果记忆：缓存工具调用结果，避免重复调用


@dataclass
class MemoryItem:
    """单条记忆数据单元。"""

    id: str = field(
        default_factory=lambda: uuid.uuid4().hex
    )  # 记忆唯一ID，用于更新或删除
    type: MemoryType = MemoryType.SESSION  # 记忆类型，决定存储策略和生命周期
    key: str = ""  # 记忆键，如 "chat_history"、"user_preference"、"order_cache"
    value: Any = None  # 记忆值，可以是字符串、字典、列表等任意类型
    session_id: str | None = None  # 所属会话ID，用于会话级隔离
    user_id: str | None = None  # 所属用户ID，用于用户级隔离
    tenant_id: str | None = None  # 所属租户ID，用于多租户数据隔离
    ttl_seconds: float | None = None  # 生存时间（秒），None 表示永不过期
    created_at: float = field(default_factory=time.time)  # 创建时间戳
    updated_at: float = field(default_factory=time.time)  # 最后更新时间戳
    access_count: int = 0  # 访问次数，可用于热度统计或淘汰策略
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据，如来源、标签等

    def is_expired(self) -> bool:
        """判断当前记忆是否已过期。"""
        if self.ttl_seconds is None:  # 无 TTL 限制则永不过期
            return False
        return (
            time.time() - self.created_at
        ) > self.ttl_seconds  # 当前时间减去创建时间超过 TTL 则过期

    def to_dict(self) -> dict[str, Any]:
        """转换为字典结构，便于序列化。"""
        return {
            "id": self.id,
            "type": self.type.value
            if isinstance(self.type, MemoryType)
            else str(self.type),
            "key": self.key,
            "value": self.value,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "ttl_seconds": self.ttl_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "access_count": self.access_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryItem":
        """从字典反序列化生成 MemoryItem 实例。"""
        raw_type = data.get("type", MemoryType.SESSION)
        if isinstance(raw_type, str):
            try:
                mem_type = MemoryType(raw_type)
            except ValueError:
                mem_type = MemoryType.SESSION
        elif isinstance(raw_type, MemoryType):
            mem_type = raw_type
        else:
            mem_type = MemoryType.SESSION

        return cls(
            id=data.get("id"),
            type=mem_type,
            key=data.get("key", ""),
            value=data.get("value"),
            session_id=data.get("session_id"),
            user_id=data.get("user_id"),
            tenant_id=data.get("tenant_id"),
            ttl_seconds=data.get("ttl_seconds"),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            access_count=data.get("access_count", 0),
            metadata=data.get("metadata") or {},
        )


@dataclass
class MemoryQuery:
    """记忆查询条件，支持多维度过滤。"""

    key: str | None = None  # 按记忆键精确匹配
    type: MemoryType | None = None  # 按记忆类型过滤
    session_id: str | None = None  # 按会话ID过滤
    user_id: str | None = None  # 按用户ID过滤
    tenant_id: str | None = None  # 按租户ID过滤
    limit: int = 50  # 最大返回数量，防止一次性加载过多数据
    include_expired: bool = False  # 是否包含已过期的记忆（默认过滤掉）


@dataclass
class MemoryResult:
    """记忆查询结果。"""

    items: list[MemoryItem]  # 匹配的记忆列表
    total_count: int  # 匹配总数（可用于分页）
    query_time_ms: int  # 查询耗时（毫秒）


class BaseMemory(ABC):
    """记忆基类：统一记忆的读写、隔离、过期清理和审计。

    设计目标：
    - 为 Agent 提供短期会话上下文、工作记忆和长期记忆的抽象；
    - 支持多租户、多用户、多会话的数据隔离；
    - 支持 TTL 过期和主动清理，防止内存无限增长；
    - 提供结构化日志，便于审计和调试。
    """

    name: str = "base_memory"  # 记忆实例名称，用于日志区分
    max_items: int = 10000  # 最大记忆条目数，超过时触发淘汰策略

    def __init__(self, name: str | None = None, max_items: int | None = None):
        self._lock = threading.RLock()  # 读写锁，保证线程安全
        self._item_count: int = 0  # 当前记忆条目计数
        if name is not None:
            self.name = name
        if max_items is not None:
            self.max_items = max_items

    def _inc_count(self):
        """增加记忆条目计数。"""
        self._item_count += 1  # 计数器加一

    def _dec_count(self):
        """减少记忆条目计数。"""
        self._item_count -= 1  # 计数器减一

    def _check_capacity(self):
        """检查是否超出容量上限，子类可覆盖实现具体淘汰策略。"""
        if self._item_count > self.max_items:  # 超过上限则触发清理
            logger.warning(
                "memory_capacity_exceeded name=%s count=%d max=%d",
                self.name,
                self._item_count,
                self.max_items,
            )
            self._evict()  # 调用淘汰方法

    @abstractmethod
    def _evict(self):
        """淘汰策略：当记忆数量超过上限时，决定删除哪些记忆。

        子类可根据业务需求实现 LRU、LFU、按 TTL 清理等策略。
        """

    @abstractmethod
    def _do_write(self, item: MemoryItem) -> MemoryItem:
        """底层写入逻辑，子类必须实现具体存储方式。

        可以是内存字典、Redis、数据库等。
        返回写入后的完整 MemoryItem（含生成的 ID 等）。
        """

    @abstractmethod
    def _do_read(self, query: MemoryQuery) -> list[MemoryItem]:
        """底层读取逻辑，子类必须实现具体查询方式。

        根据查询条件从存储中检索记忆。
        """

    @abstractmethod
    def _do_delete(self, memory_id: str) -> bool:
        """底层删除逻辑，子类必须实现。

        按记忆 ID 删除单条记忆，返回是否删除成功。
        """

    def write(self, item: MemoryItem) -> MemoryItem:
        """写入或更新一条记忆。"""
        with self._lock:  # 加锁保证线程安全
            item.updated_at = time.time()  # 更新最后访问时间
            if not item.id:  # 如果没有 ID 则生成新的
                item.id = uuid.uuid4().hex
            result = self._do_write(item)  # 调用子类实现的底层写入
            self._inc_count()  # 增加计数
            self._check_capacity()  # 检查容量
            logger.info(
                "memory_write name=%s id=%s key=%s type=%s user=%s session=%s",
                self.name,
                result.id,
                result.key,
                result.type.value,
                result.user_id,
                result.session_id,
            )
            return result

    def read(self, query: MemoryQuery) -> MemoryResult:
        """根据查询条件读取记忆。"""
        with self._lock:  # 加锁保证线程安全
            start = time.time()  # 记录查询开始时间
            items = self._do_read(query)  # 调用子类实现的底层读取

            # 过滤过期记忆
            if not query.include_expired:
                items = [item for item in items if not item.is_expired()]

            # 更新访问次数
            for item in items:
                item.access_count += 1  # 每次读取增加访问计数

            duration = int((time.time() - start) * 1000)  # 计算查询耗时
            result = MemoryResult(
                items=items,  # 匹配的记忆列表
                total_count=len(items),  # 匹配数量
                query_time_ms=duration,  # 查询耗时
            )
            logger.info(
                "memory_read name=%s count=%d duration_ms=%d key=%s type=%s",
                self.name,
                result.total_count,
                duration,
                query.key,
                query.type.value if query.type else None,
            )
            return result

    def delete(self, memory_id: str) -> bool:
        """删除指定 ID 的记忆。"""
        with self._lock:  # 加锁保证线程安全
            success = self._do_delete(memory_id)  # 调用子类实现的底层删除
            if success:
                self._dec_count()  # 删除成功则减少计数
                logger.info("memory_delete name=%s id=%s", self.name, memory_id)
            return success

    def clear_session(self, session_id: str) -> int:
        """清空指定会话的所有记忆，返回删除数量。"""
        query = MemoryQuery(
            session_id=session_id, include_expired=True
        )  # 查询该会话所有记忆
        items = self._do_read(query)  # 获取所有匹配项
        count = 0  # 删除计数器
        for item in items:
            if self._do_delete(item.id):  # 逐条删除
                count += 1
                self._dec_count()
        logger.info(
            "memory_clear_session name=%s session=%s deleted=%d",
            self.name,
            session_id,
            count,
        )
        return count

    def clear_user(self, user_id: str) -> int:
        """清空指定用户的所有记忆，返回删除数量。"""
        query = MemoryQuery(user_id=user_id, include_expired=True)  # 查询该用户所有记忆
        items = self._do_read(query)  # 获取所有匹配项
        count = 0  # 删除计数器
        for item in items:
            if self._do_delete(item.id):  # 逐条删除
                count += 1
                self._dec_count()
        logger.info(
            "memory_clear_user name=%s user=%s deleted=%d", self.name, user_id, count
        )
        return count

    def get_stats(self) -> dict[str, Any]:
        """获取记忆统计信息，用于监控和调试。"""
        with self._lock:
            return {
                "name": self.name,  # 记忆实例名称
                "item_count": self._item_count,  # 当前条目数
                "max_items": self.max_items,  # 容量上限
            }


class BaseMessageStore(ABC):
    @abstractmethod
    async def get_messages(self, session_id: str):
        pass

    @abstractmethod
    async def add_message(self, session_id: str, message):
        pass

    @abstractmethod
    async def clear(self, session_id: str):
        pass
