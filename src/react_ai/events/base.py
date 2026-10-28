import json
import logging
import threading
import time
import uuid
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class EventLevel(Enum):
    """事件级别枚举，用于区分事件的重要程度。"""
    DEBUG = "debug"          # 调试级别：开发排查用，生产环境通常忽略
    INFO = "info"            # 信息级别：常规业务流转记录
    WARNING = "warning"      # 警告级别：潜在风险，但不影响主流程
    ERROR = "error"          # 错误级别：业务异常，需要关注
    CRITICAL = "critical"    # 严重级别：系统级故障，需要立即处理


class EventScope(Enum):
    """事件作用域枚举，用于区分事件的传播范围。"""
    LOCAL = "local"              # 本地事件：仅当前进程内消费
    PROCESS = "process"          # 进程内事件：同进程内多模块订阅
    CLUSTER = "cluster"          # 集群事件：多实例间广播（需消息队列支持）
    CROSS_TENANT = "cross_tenant"  # 跨租户事件：涉及多租户数据流转


@dataclass
class EventHeader:
    """事件头信息，携带元数据。"""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 事件唯一ID，用于幂等和追踪
    trace_id: str = ""  # 链路追踪ID，串联全链路日志
    span_id: str = ""  # 当前操作跨度ID，细化 trace 内的调用链
    parent_event_id: str = ""  # 父事件ID，用于构建事件因果树
    timestamp: float = field(default_factory=time.time)  # 事件发生时间戳
    source: str = ""  # 事件来源，如模块名、服务名、Agent 名称
    version: str = "1.0"  # 事件协议版本，用于兼容升级
    retry_count: int = 0  # 当前重试次数，用于重试上限控制
    max_retries: int = 3  # 最大重试次数，超过则进入死信


@dataclass
class EventPayload:
    """事件载荷，承载业务数据。"""
    event_type: str  # 事件类型，如 "agent.started"、"tool.completed"
    data: dict[str, Any] = field(default_factory=dict)  # 业务数据，如输入参数、输出结果
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据，如标签、环境信息
    schema_version: str = "1.0"  # 数据 schema 版本，用于反序列化兼容


@dataclass
class EventContext:
    """事件上下文，携带环境信息。"""
    tenant_id: str | None = None  # 租户ID，多租户隔离
    user_id: str | None = None  # 用户ID，标识触发者
    session_id: str | None = None  # 会话ID，关联对话或操作流程
    agent_id: str | None = None  # Agent ID，标识执行 Agent
    task_id: str | None = None  # 任务ID，标识当前任务
    environment: str = "production"  # 环境标识，如 dev / staging / production
    ip_address: str | None = None  # 触发者 IP 地址
    user_agent: str | None = None  # 触发者客户端信息


@dataclass
class EventEnvelope:
    """事件完整信封，包含头、载荷和上下文。"""
    header: EventHeader  # 事件头
    payload: EventPayload  # 事件载荷
    context: EventContext  # 事件上下文
    level: EventLevel = EventLevel.INFO  # 事件级别
    scope: EventScope = EventScope.LOCAL  # 事件作用域
    tags: list[str] = field(default_factory=list)  # 事件标签，用于分类和过滤
    serialized: str = ""  # 序列化后的字符串，用于存储或传输

    def serialize(self) -> str:
        """将事件序列化为 JSON 字符串。"""
        data = {
            "header": {
                "event_id": self.header.event_id,
                "trace_id": self.header.trace_id,
                "span_id": self.header.span_id,
                "parent_event_id": self.header.parent_event_id,
                "timestamp": self.header.timestamp,
                "source": self.header.source,
                "version": self.header.version,
                "retry_count": self.header.retry_count,
                "max_retries": self.header.max_retries,
            },
            "payload": {
                "event_type": self.payload.event_type,
                "data": self.payload.data,
                "metadata": self.payload.metadata,
                "schema_version": self.payload.schema_version,
            },
            "context": {
                "tenant_id": self.context.tenant_id,
                "user_id": self.context.user_id,
                "session_id": self.context.session_id,
                "agent_id": self.context.agent_id,
                "task_id": self.context.task_id,
                "environment": self.context.environment,
                "ip_address": self.context.ip_address,
                "user_agent": self.context.user_agent,
            },
            "level": self.level.value if isinstance(self.level, EventLevel) else str(self.level),
            "scope": self.scope.value if isinstance(self.scope, EventScope) else str(self.scope),
            "tags": self.tags,
        }
        self.serialized = json.dumps(data, ensure_ascii=False, default=str)  # 序列化为 JSON
        return self.serialized

    @classmethod
    def deserialize(cls, data: str) -> "EventEnvelope":
        """从 JSON 字符串反序列化事件。"""
        obj = json.loads(data)  # 解析 JSON
        header_data = obj.get("header", {})
        header = EventHeader(
            event_id=header_data.get("event_id", uuid.uuid4().hex),
            trace_id=header_data.get("trace_id", ""),
            span_id=header_data.get("span_id", ""),
            parent_event_id=header_data.get("parent_event_id", ""),
            timestamp=header_data.get("timestamp", time.time()),
            source=header_data.get("source", ""),
            version=header_data.get("version", "1.0"),
            retry_count=header_data.get("retry_count", 0),
            max_retries=header_data.get("max_retries", 3),
        )
        payload_data = obj.get("payload", {})
        payload = EventPayload(
            event_type=payload_data.get("event_type", ""),
            data=payload_data.get("data", {}),
            metadata=payload_data.get("metadata", {}),
            schema_version=payload_data.get("schema_version", "1.0"),
        )
        ctx = obj.get("context", {})
        context = EventContext(
            tenant_id=ctx.get("tenant_id"),
            user_id=ctx.get("user_id"),
            session_id=ctx.get("session_id"),
            agent_id=ctx.get("agent_id"),
            task_id=ctx.get("task_id"),
            environment=ctx.get("environment", "production"),
            ip_address=ctx.get("ip_address"),
            user_agent=ctx.get("user_agent"),
        )
        raw_level = obj.get("level", "info")
        try:
            level = EventLevel(raw_level)
        except ValueError:
            level = EventLevel.INFO

        raw_scope = obj.get("scope", "local")
        try:
            scope = EventScope(raw_scope)
        except ValueError:
            scope = EventScope.LOCAL

        envelope = cls(
            header=header,
            payload=payload,
            context=context,
            level=level,
            scope=scope,
            tags=obj.get("tags", []),
            serialized=data,
        )
        return envelope


class BaseEvent(ABC):
    """事件基类：统一事件的构建、发布、订阅、重试和审计。

    设计目标：
    - 为 Agent 提供结构化的事件总线抽象，支持模块间解耦通信；
    - 统一事件格式，包含链路追踪、租户隔离、重试机制；
    - 支持同步/异步发布，本地/集群多作用域；
    - 内置事件过滤和幂等检查，避免重复消费；
    - 输出结构化日志，便于事件溯源和问题排查。
    """

    name: str = "base_event"  # 事件总线名称，用于日志区分
    max_retry_delay: float = 60.0  # 最大重试延迟（秒）

    def __init__(self, name: str | None = None, max_retry_delay: float | None = None):
        self._lock = threading.RLock()  # 线程锁，保护共享状态
        self._subscribers: dict[str, list[Callable[[EventEnvelope], Any]]] = {}  # 订阅者注册表：event_type -> [handler, ...]
        self._published_count: int = 0  # 累计发布事件数
        self._consumed_count: int = 0  # 累计消费事件数
        self._failed_count: int = 0  # 累计消费失败数
        self._processed_ids: set[str] = set()  # 已处理事件ID集合，用于幂等
        if name is not None:
            self.name = name
        if max_retry_delay is not None:
            self.max_retry_delay = max_retry_delay

    def subscribe(self, event_type: str, handler: Callable[[EventEnvelope], Any]):
        """订阅指定类型的事件。

        Args:
            event_type: 事件类型，如 "agent.started"
            handler: 回调函数，接收 EventEnvelope 参数
        """
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            if handler not in self._subscribers[event_type]:
                self._subscribers[event_type].append(handler)
            logger.info("event_subscribe name=%s type=%s handlers=%d", self.name, event_type, len(self._subscribers[event_type]))

    def unsubscribe(self, event_type: str, handler: Callable[[EventEnvelope], Any]):
        """取消订阅指定类型的事件。

        Args:
            event_type: 事件类型
            handler: 要移除的回调函数
        """
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(handler)
                    logger.info("event_unsubscribe name=%s type=%s", self.name, event_type)
                except ValueError:
                    pass  # 处理器不在列表中，忽略

    def publish(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        context: EventContext | None = None,
        level: EventLevel = EventLevel.INFO,
        scope: EventScope = EventScope.LOCAL,
        tags: list[str] | None = None,
        trace_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        """发布一个事件。

        Args:
            event_type: 事件类型
            data: 业务数据
            context: 事件上下文
            level: 事件级别
            scope: 事件作用域
            tags: 事件标签
            trace_id: 链路追踪ID，不传则自动生成
            parent_event_id: 父事件ID，用于因果链

        Returns:
            构建好的 EventEnvelope
        """
        context = context or EventContext()  # 使用传入上下文或新建
        data = data or {}  # 确保数据不为 None
        tags = tags or []  # 确保标签不为 None

        # 构建事件头
        header = EventHeader(
            trace_id=trace_id or uuid.uuid4().hex,
            parent_event_id=parent_event_id or "",
            source=self.name,
        )

        # 构建载荷
        payload = EventPayload(event_type=event_type, data=data)

        # 构建信封
        envelope = EventEnvelope(
            header=header,
            payload=payload,
            context=context,
            level=level,
            scope=scope,
            tags=tags,
        )

        # 序列化
        envelope.serialize()

        # 发布前钩子
        self._before_publish(envelope)

        # 记录发布
        with self._lock:
            self._published_count += 1

        # 分发到订阅者
        self._dispatch(envelope)

        # 发布后钩子
        self._after_publish(envelope)

        logger.info(
            "event_publish name=%s type=%s id=%s trace=%s level=%s scope=%s",
            self.name, event_type, envelope.header.event_id,
            envelope.header.trace_id, level.value, scope.value,
        )

        return envelope

    def _dispatch(self, envelope: EventEnvelope):
        """将事件分发到匹配的订阅者。

        Args:
            envelope: 事件信封
        """
        event_type = envelope.payload.event_type  # 获取事件类型
        handlers: list[Callable[[EventEnvelope], Any]] = []

        with self._lock:
            # 查找精确匹配的订阅者
            if event_type in self._subscribers:
                handlers.extend(self._subscribers[event_type])
            # 查找通配符订阅（如 "agent.*" 匹配 "agent.started"）
            for pattern, pattern_handlers in self._subscribers.items():
                if pattern.endswith(".*") and event_type.startswith(pattern[:-1]):
                    handlers.extend(pattern_handlers)

        # 幂等检查：已处理过的事件跳过
        if self._is_duplicate(envelope):
            logger.info("event_duplicate_skip id=%s type=%s", envelope.header.event_id, event_type)
            return

        # 逐个调用处理器
        for handler in handlers:
            self._invoke_handler(handler, envelope)

    def _invoke_handler(self, handler: Callable[[EventEnvelope], Any], envelope: EventEnvelope):
        """调用单个订阅处理器，带异常捕获和重试。

        Args:
            handler: 回调函数
            envelope: 事件信封
        """
        try:
            handler(envelope)  # 调用处理器
            self._mark_processed(envelope)  # 标记为已处理
            with self._lock:
                self._consumed_count += 1
            logger.debug(
                "event_consumed handler=%s type=%s id=%s",
                handler.__name__ if hasattr(handler, "__name__") else str(handler),
                envelope.payload.event_type,
                envelope.header.event_id,
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._failed_count += 1
            logger.error(
                "event_handler_error handler=%s type=%s id=%s error=%s",
                handler.__name__ if hasattr(handler, "__name__") else str(handler),
                envelope.payload.event_type,
                envelope.header.event_id,
                str(exc),
            )
            self._on_handler_error(handler, envelope, exc)

    def _is_duplicate(self, envelope: EventEnvelope) -> bool:
        """检查事件是否已处理过（幂等）。

        Args:
            envelope: 事件信封

        Returns:
            是否重复
        """
        with self._lock:
            return envelope.header.event_id in self._processed_ids

    def _mark_processed(self, envelope: EventEnvelope):
        """标记事件为已处理。

        Args:
            envelope: 事件信封
        """
        with self._lock:
            # 防止集合无限增长，达到上限时清理一半
            if len(self._processed_ids) >= 10000:
                to_remove = list(self._processed_ids)[:5000]
                for eid in to_remove:
                    self._processed_ids.discard(eid)
            self._processed_ids.add(envelope.header.event_id)

    def _before_publish(self, envelope: EventEnvelope):
        """发布前钩子，子类可覆盖实现校验或增强。

        Args:
            envelope: 事件信封
        """

    def _after_publish(self, envelope: EventEnvelope):
        """发布后钩子，子类可覆盖实现异步转发或持久化。

        Args:
            envelope: 事件信封
        """

    def _on_handler_error(self, handler: Callable[[EventEnvelope], Any], envelope: EventEnvelope, error: Exception):
        """处理器错误回调，子类可覆盖实现重试或死信队列。

        Args:
            handler: 出错的处理器
            envelope: 事件信封
            error: 异常对象
        """

    def get_stats(self) -> dict[str, Any]:
        """获取事件总线统计信息，用于监控。"""
        with self._lock:
            subscriber_count = sum(len(h) for h in self._subscribers.values())
            return {
                "name": self.name,  # 总线名称
                "published_count": self._published_count,  # 发布数
                "consumed_count": self._consumed_count,  # 消费数
                "failed_count": self._failed_count,  # 失败数
                "subscriber_count": subscriber_count,  # 订阅者数
                "processed_id_cache_size": len(self._processed_ids),  # 幂等缓存大小
                "event_types": list(self._subscribers.keys()),  # 已注册事件类型
            }

    def clear_processed_cache(self):
        """清空已处理事件ID缓存，释放内存。"""
        with self._lock:
            self._processed_ids.clear()
            logger.info("event_cache_cleared name=%s", self.name)
