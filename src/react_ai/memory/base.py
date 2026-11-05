import hashlib
import json
import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from react_ai.observability import metrics, tracer
from react_ai.schemas.messages import ChatMessage, MessageRole
from react_ai.utils.token_counter import (
    estimate_messages_tokens,
    trim_messages_to_token_budget,
)

logger = logging.getLogger(__name__)


def _elapsed_ms(start: float) -> float:
    """计算从 start（perf_counter）至今的毫秒耗时."""
    return (time.perf_counter() - start) * 1000


# 会话历史记忆的统一键名（load_session / save_turn 专用）
SESSION_HISTORY_KEY = "chat_history"
# 工作记忆的统一键名（save_working_state / load_working_state 专用）
WORKING_STATE_KEY = "working_state"
# 工具结果记忆的键名前缀（save_tool_result / load_tool_result 专用）
TOOL_RESULT_KEY_PREFIX = "tool_result"

# 匿名兜底命名空间：缺失身份时独立隔离，不与任何已认证身份共享会话
ANONYMOUS_TENANT_ID = "anonymous"
ANONYMOUS_USER_ID = "anonymous"


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


@dataclass(frozen=True)
class SessionKey:
    """会话身份三元组（差距分析 2.2）：记忆会话访问必须以
    (tenant_id, user_id, session_id) 联合定位，禁止裸 session_id 查询，
    从根上杜绝跨用户/跨租户的数据串扰。"""

    tenant_id: str
    user_id: str
    session_id: str

    def __post_init__(self):
        # 三个维度都必须是非空字符串，缺失即拒绝，不允许退化为部分隔离
        for field_name in ("tenant_id", "user_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"SessionKey.{field_name} 不能为空，禁止裸 session_id 查询"
                )

    def composite(self) -> str:
        """组合为单一字符串键，供仅接受 str 的存储层使用。"""
        return f"{self.tenant_id}::{self.user_id}::{self.session_id}"

    @classmethod
    def resolve(
        cls,
        tenant_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> "SessionKey":
        """解析会话身份：session_id 必填（缺失抛 ValueError）；
        tenant/user 缺失时落入独立的匿名命名空间，不与任何已认证身份共享。"""
        if not session_id or not str(session_id).strip():
            raise ValueError("session_id 为必填项，生产环境禁止 default-session 兜底")
        return cls(
            tenant_id=(tenant_id or ANONYMOUS_TENANT_ID).strip(),
            user_id=(user_id or ANONYMOUS_USER_ID).strip(),
            session_id=str(session_id).strip(),
        )


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

    def __init__(
        self,
        name: str | None = None,
        max_items: int | None = None,
        session_ttl_seconds: float | None = None,
        history_token_budget: int | None = None,
        enforce_tenant_isolation: bool = True,
    ):
        self._lock = threading.RLock()  # 读写锁，保证线程安全
        self._item_count: int = 0  # 当前记忆条目计数
        # 会话记忆条目的 TTL（秒），None 表示永不过期；过期会话自动开新会话
        self.session_ttl_seconds = session_ttl_seconds
        # 会话历史 token 预算（差距分析 2.5）：None 或 <=0 表示不启用裁剪
        self.history_token_budget = history_token_budget
        # 租户强制隔离闸（差距分析 2.6）：True 时禁止 tenant_id=None 的跨租户读写，
        # 生产环境默认启用；关闭仅用于开发/测试场景。
        self.enforce_tenant_isolation = enforce_tenant_isolation
        # 预算截断事件计数，供 get_stats 暴露（事件次数 / 累计丢弃条数）
        self._token_truncated_events: int = 0
        self._token_truncated_messages: int = 0
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

    def _record_op(
        self,
        op: str,
        tenant_id: str | None,
        duration_ms: float,
        *,
        result: str = "ok",
        items: int = 0,
    ) -> None:
        """上报记忆操作指标（差距分析 2.8）；指标层异常不得阻断主流程。"""
        try:
            metrics.record_memory_op(
                op=op,
                backend=self.name,
                tenant_id=tenant_id or "",
                result=result,
                duration_ms=duration_ms,
                items=items,
            )
        except Exception:  # pragma: no cover - 指标上报失败仅降级
            logger.debug("memory_metric_emit_failed op=%s", op)

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
            # 淘汰频率可观测（差距分析 2.8）
            try:
                metrics.record_memory_eviction(self.name, 1)
            except Exception:  # pragma: no cover
                pass

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
        start = time.perf_counter()
        ok = False
        type_label = (
            item.type.value if isinstance(item.type, MemoryType) else str(item.type)
        )
        try:
            # 链路追踪（差距分析 2.8）：memory.write span
            with tracer.span(
                "memory.write",
                {
                    "backend": self.name,
                    "key": item.key,
                    "type": type_label,
                    "tenant_id": item.tenant_id or "",
                    "user_id": item.user_id or "",
                    "session_id": item.session_id or "",
                },
            ):
                if self.enforce_tenant_isolation and not item.tenant_id:
                    raise ValueError(
                        "tenant_id 为必填项，禁止写入无租户条（差距分析 2.6）"
                    )
                with self._lock:  # 加锁保证线程安全
                    item.updated_at = time.time()  # 更新最后访问时间
                    if not item.id:  # 如果没有 ID 则生成新的
                        item.id = uuid.uuid4().hex
                    result = self._do_write(item)  # 调用子类实现的底层写入
                    self._inc_count()  # 增加计数
                    self._check_capacity()  # 检查容量
                    logger.info(
                        "memory_write name=%s id=%s key=%s type=%s user=%s session=%s tenant=%s",
                        self.name,
                        result.id,
                        result.key,
                        result.type.value,
                        result.user_id,
                        result.session_id,
                        result.tenant_id,
                    )
                    ok = True
                    return result
        finally:
            self._record_op(
                "write",
                item.tenant_id,
                _elapsed_ms(start),
                result="ok" if ok else "error",
                items=1 if ok else 0,
            )

    def read(self, query: MemoryQuery) -> MemoryResult:
        """根据查询条件读取记忆。"""
        start = time.perf_counter()
        ok = False
        count = 0
        type_label = query.type.value if query.type else ""
        try:
            # 链路追踪（差距分析 2.8）：memory.read span
            with tracer.span(
                "memory.read",
                {
                    "backend": self.name,
                    "key": query.key or "",
                    "type": type_label,
                    "tenant_id": query.tenant_id or "",
                    "user_id": query.user_id or "",
                    "session_id": query.session_id or "",
                },
            ) as span:
                if self.enforce_tenant_isolation and query.tenant_id is None:
                    raise ValueError(
                        "tenant_id 为必填项，禁止跨租户无界查询（差距分析 2.6）"
                    )
                with self._lock:  # 加锁保证线程安全
                    start_inner = time.time()  # 记录查询开始时间
                    items = self._do_read(query)  # 调用子类实现的底层读取

                    # 过滤过期记忆
                    if not query.include_expired:
                        items = [item for item in items if not item.is_expired()]

                    # 更新访问次数
                    for item in items:
                        item.access_count += 1  # 每次读取增加访问计数

                    duration = int((time.time() - start_inner) * 1000)  # 查询耗时
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
                        type_label or None,
                    )
                count = result.total_count
                span.set_attribute("result_count", count)
                ok = True
                return result
        finally:
            # 命中率指标：hit/miss 供 memory_hit_rate 聚合（差距分析 2.8）
            if not ok:
                label = "error"
            else:
                label = "hit" if count else "miss"
            self._record_op(
                "read",
                query.tenant_id,
                _elapsed_ms(start),
                result=label,
                items=count,
            )

    def delete(self, memory_id: str) -> bool:
        """删除指定 ID 的记忆。"""
        start = time.perf_counter()
        success = False
        try:
            # 链路追踪（差距分析 2.8）：memory.delete span
            with tracer.span("memory.delete", {"backend": self.name, "memory_id": memory_id}):
                with self._lock:  # 加锁保证线程安全
                    success = self._do_delete(memory_id)  # 调用子类实现的底层删除
                    if success:
                        self._dec_count()  # 删除成功则减少计数
                        logger.info("memory_delete name=%s id=%s", self.name, memory_id)
                    return success
        finally:
            self._record_op(
                "delete",
                None,
                _elapsed_ms(start),
                result="ok" if success else "miss",
                items=1 if success else 0,
            )

    def clear_session(self, session_id: str, tenant_id: str | None = None) -> int:
        """清空指定会话的所有记忆，返回删除数量。

        差距分析 2.6：enforce_tenant_isolation=True 时必须显式传入 tenant_id，
        防止仅靠裸 session_id 跨租户清除他人数据。
        """
        if self.enforce_tenant_isolation and not tenant_id:
            raise ValueError(
                "tenant_id 为必填项，clear_session 禁止跨租户清除（差距分析 2.6）"
            )
        start = time.perf_counter()
        count = 0
        ok = False
        try:
            # 链路追踪（差距分析 2.8）：memory.clear_session span
            with tracer.span(
                "memory.clear_session",
                {"backend": self.name, "session_id": session_id, "tenant_id": tenant_id or ""},
            ):
                query = MemoryQuery(
                    session_id=session_id, tenant_id=tenant_id, include_expired=True
                )
                # 走 self.read 统一 tenant 闸，而不是直接 _do_read
                items = self.read(query).items
                for item in items:
                    if self.delete(item.id):  # delete 内部走 _do_delete + _dec_count
                        count += 1
                logger.info(
                    "memory_clear_session name=%s session=%s tenant=%s deleted=%d",
                    self.name,
                    session_id,
                    tenant_id,
                    count,
                )
                ok = True
                return count
        finally:
            self._record_op(
                "clear_session",
                tenant_id,
                _elapsed_ms(start),
                result="ok" if ok else "error",
                items=count,
            )

    def clear_user(self, user_id: str, tenant_id: str | None = None) -> int:
        """清空指定用户的所有记忆，返回删除数量。

        差距分析 2.6：enforce_tenant_isolation=True 时必须显式传入 tenant_id，
        防止仅靠裸 user_id 跨租户清除他人数据。
        """
        if self.enforce_tenant_isolation and not tenant_id:
            raise ValueError(
                "tenant_id 为必填项，clear_user 禁止跨租户清除（差距分析 2.6）"
            )
        start = time.perf_counter()
        count = 0
        ok = False
        try:
            # 链路追踪（差距分析 2.8）：memory.clear_user span
            with tracer.span(
                "memory.clear_user",
                {"backend": self.name, "user_id": user_id, "tenant_id": tenant_id or ""},
            ):
                query = MemoryQuery(user_id=user_id, tenant_id=tenant_id, include_expired=True)
                items = self.read(query).items
                for item in items:
                    if self.delete(item.id):
                        count += 1
                logger.info(
                    "memory_clear_user name=%s user=%s tenant=%s deleted=%d",
                    self.name,
                    user_id,
                    tenant_id,
                    count,
                )
                ok = True
                return count
        finally:
            self._record_op(
                "clear_user",
                tenant_id,
                _elapsed_ms(start),
                result="ok" if ok else "error",
                items=count,
            )

    # ------------------------------------------------------------------
    # 会话级读写：必须经 SessionKey 身份三元组定位（差距分析 2.2）
    # ------------------------------------------------------------------

    def load_session(
        self,
        key: SessionKey,
        max_messages: int = 200,
        token_budget: int | None = None,
    ) -> list[ChatMessage]:
        """按身份三元组加载会话历史消息。

        - tenant/user/session 任一维度不匹配即不可见，保证不同身份下
          相同 session_id 字符串互不串扰；
        - 过期条目由 TTL 过滤，会话过期后自然从头开始；
        - 先按条数取最近 max_messages 条，再按 token 预算从最旧消息开始
          丢弃（差距分析 2.5）。预算优先级：调用级 token_budget >
          实例级 history_token_budget；两者均未启用（None/<=0）时行为
          与改造前一致。
        """
        if not isinstance(key, SessionKey):
            raise TypeError("load_session 必须使用 SessionKey 身份三元组，禁止裸 session_id 查询")
        start = time.perf_counter()
        ok = False
        try:
            # 链路追踪（差距分析 2.8）：memory.load span
            with tracer.span(
                "memory.load",
                {
                    "backend": self.name,
                    "tenant_id": key.tenant_id,
                    "user_id": key.user_id,
                    "session_id": key.session_id,
                    "max_messages": max_messages,
                },
            ) as span:
                result = self.read(
                    MemoryQuery(
                        key=SESSION_HISTORY_KEY,
                        type=MemoryType.SESSION,
                        session_id=key.session_id,
                        user_id=key.user_id,
                        tenant_id=key.tenant_id,
                        limit=0,  # 存储层不截断，此处取最近 max_messages 条
                    )
                )
                messages: list[ChatMessage] = []
                for item in result.items:
                    try:
                        messages.append(ChatMessage.model_validate(item.value))
                    except Exception:
                        logger.warning(
                            "memory_session_message_deserialize_failed id=%s", item.id
                        )
                messages = messages[-max_messages:] if max_messages > 0 else messages

                effective_budget = (
                    token_budget if token_budget is not None else self.history_token_budget
                )
                if effective_budget is not None and effective_budget > 0:
                    kept, dropped = trim_messages_to_token_budget(messages, effective_budget)
                    if dropped:
                        with self._lock:
                            self._token_truncated_events += 1
                            self._token_truncated_messages += dropped
                        logger.warning(
                            "memory_token_budget_truncated name=%s session=%s budget=%d "
                            "dropped=%d kept=%d kept_tokens=%d",
                            self.name,
                            key.session_id,
                            effective_budget,
                            dropped,
                            len(kept),
                            estimate_messages_tokens(kept),
                        )
                    messages = kept
                span.set_attribute("message_count", len(messages))
                if effective_budget is not None and effective_budget > 0:
                    span.set_attribute("token_budget", effective_budget)
                ok = True
                return messages
        finally:
            self._record_op(
                "load",
                key.tenant_id,
                _elapsed_ms(start),
                result=("hit" if messages else "miss") if ok else "error",
                items=len(messages) if ok else 0,
            )

    def save_turn(self, key: SessionKey, messages: list[ChatMessage]) -> int:
        """回写本轮对话到会话记忆（按 SessionKey 打身份标签并附加 TTL）。

        仅持久化 user/assistant 的纯文本消息；含 tool_calls 的中间消息与
        tool 观测不落库，避免下一轮上下文回放残缺的工具协议。
        返回实际写入的消息条数。
        """
        if not isinstance(key, SessionKey):
            raise TypeError("save_turn 必须使用 SessionKey 身份三元组，禁止裸 session_id 写入")
        start = time.perf_counter()
        saved = 0
        ok = False
        try:
            # 链路追踪（差距分析 2.8）：memory.save span
            with tracer.span(
                "memory.save",
                {
                    "backend": self.name,
                    "tenant_id": key.tenant_id,
                    "user_id": key.user_id,
                    "session_id": key.session_id,
                },
            ) as span:
                for msg in messages:
                    if msg.role not in (MessageRole.USER, MessageRole.ASSISTANT):
                        continue
                    if not msg.content or msg.tool_calls:
                        continue
                    self.write(
                        MemoryItem(
                            type=MemoryType.SESSION,
                            key=SESSION_HISTORY_KEY,
                            value={"role": msg.role.value, "content": msg.content},
                            session_id=key.session_id,
                            user_id=key.user_id,
                            tenant_id=key.tenant_id,
                            ttl_seconds=self.session_ttl_seconds,
                        )
                    )
                    saved += 1
                span.set_attribute("saved_count", saved)
                ok = True
                return saved
        finally:
            self._record_op(
                "save",
                key.tenant_id,
                _elapsed_ms(start),
                result="ok" if ok else "error",
                items=saved,
            )

    # ------------------------------------------------------------------
    # 工作记忆读写：按 run_id 记录任务执行中间状态（差距分析 2.7 自动沉淀）
    # ------------------------------------------------------------------

    def save_working_state(
        self,
        key: SessionKey,
        run_id: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: float | None = None,
    ) -> MemoryItem:
        """写入/更新当前运行的工作记忆快照。

        使用 run_id 派生的确定性 ID，同一运行多次写入为原地更新而非追加，
        保证"最新中间状态"语义；TTL 防止孤儿运行状态永久驻留。
        """
        if not isinstance(key, SessionKey):
            raise TypeError("save_working_state 必须使用 SessionKey 身份三元组")
        item = MemoryItem(
            id=f"working::{key.composite()}::{run_id}",
            type=MemoryType.WORKING,
            key=WORKING_STATE_KEY,
            value=payload,
            session_id=key.session_id,
            user_id=key.user_id,
            tenant_id=key.tenant_id,
            ttl_seconds=ttl_seconds,
            metadata={"run_id": run_id},
        )
        return self.write(item)

    def load_working_state(self, key: SessionKey, run_id: str) -> MemoryItem | None:
        """按身份三元组 + run_id 读取最新工作记忆快照，不存在返回 None。"""
        if not isinstance(key, SessionKey):
            raise TypeError("load_working_state 必须使用 SessionKey 身份三元组")
        result = self.read(
            MemoryQuery(
                key=WORKING_STATE_KEY,
                type=MemoryType.WORKING,
                session_id=key.session_id,
                user_id=key.user_id,
                tenant_id=key.tenant_id,
                limit=0,
            )
        )
        for item in reversed(result.items):
            if item.metadata.get("run_id") == run_id:
                return item
        return None

    # ------------------------------------------------------------------
    # 工具结果记忆读写：按 工具名+参数 缓存成功结果（差距分析 2.7 自动沉淀）
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_result_key(tool_name: str, arguments: dict[str, Any]) -> str:
        """派生工具结果记忆键：tool_result:{工具名}:{参数指纹}。

        参数按 key 排序后哈希，保证同参数不同顺序命中同一键。
        """
        try:
            canonical = json.dumps(
                arguments, sort_keys=True, ensure_ascii=False, default=str
            )
        except (TypeError, ValueError):
            canonical = str(arguments)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"{TOOL_RESULT_KEY_PREFIX}:{tool_name}:{digest}"

    def save_tool_result(
        self,
        key: SessionKey,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        ttl_seconds: float | None = None,
        tool_call_id: str | None = None,
    ) -> MemoryItem:
        """写入一条工具调用成功结果，供跨轮次/跨会话按需召回。"""
        if not isinstance(key, SessionKey):
            raise TypeError("save_tool_result 必须使用 SessionKey 身份三元组")
        item = MemoryItem(
            type=MemoryType.TOOL_RESULT,
            key=self._tool_result_key(tool_name, arguments),
            value={"tool": tool_name, "arguments": arguments, "result": result},
            session_id=key.session_id,
            user_id=key.user_id,
            tenant_id=key.tenant_id,
            ttl_seconds=ttl_seconds,
            metadata={"tool_call_id": tool_call_id} if tool_call_id else {},
        )
        return self.write(item)

    def load_tool_result(
        self, key: SessionKey, tool_name: str, arguments: dict[str, Any]
    ) -> MemoryItem | None:
        """按身份三元组 + 工具名 + 参数召回最近一次成功的工具结果，未命中返回 None。"""
        if not isinstance(key, SessionKey):
            raise TypeError("load_tool_result 必须使用 SessionKey 身份三元组")
        result = self.read(
            MemoryQuery(
                key=self._tool_result_key(tool_name, arguments),
                type=MemoryType.TOOL_RESULT,
                session_id=key.session_id,
                user_id=key.user_id,
                tenant_id=key.tenant_id,
                limit=0,
            )
        )
        return result.items[-1] if result.items else None

    def get_stats(self) -> dict[str, Any]:
        """获取记忆统计信息，用于监控和调试。"""
        with self._lock:
            return {
                "name": self.name,  # 记忆实例名称
                "item_count": self._item_count,  # 当前条目数
                "max_items": self.max_items,  # 容量上限
                # token 预算截断指标（差距分析 2.5）
                "history_token_budget": self.history_token_budget,
                "token_truncated_events": self._token_truncated_events,
                "token_truncated_messages": self._token_truncated_messages,
                # 租户强制隔离闸（差距分析 2.6）
                "enforce_tenant_isolation": self.enforce_tenant_isolation,
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
